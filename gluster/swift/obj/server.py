# Copyright (c) 2012-2014 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Object Server for Gluster for Swift """
import errno
import os

from swift.common.swob import HTTPConflict, HTTPNotImplemented
from swift.common.utils import public, timing_stats, replication, mkdirs
from swift.common.request_helpers import split_and_validate_path
from swift.obj import server

from gluster.swift.obj.diskfile import DiskFileManager
from gluster.swift.common.fs_utils import do_ismount
from gluster.swift.common.ring import Ring
from gluster.swift.common.exceptions import AlreadyExistsAsFile, \
    AlreadyExistsAsDir


class GlusterSwiftDiskFileRouter(object):
    """
    Replacement for Swift's DiskFileRouter object.
    Always returns GlusterSwift's DiskFileManager implementation.
    """
    def __init__(self, *args, **kwargs):
        self.manager_cls = DiskFileManager(*args, **kwargs)

    def __getitem__(self, policy):
        return self.manager_cls


class ObjectController(server.ObjectController):
    """
    Subclass of the object server's ObjectController which replaces the
    container_update method with one that is a no-op (information is simply
    stored on disk and already updated by virtue of performing the file system
    operations directly).
    """
    def setup(self, conf):
        """
        Implementation specific setup. This method is called at the very end
        by the constructor to allow a specific implementation to modify
        existing attributes or add its own attributes.

        :param conf: WSGI configuration parameter
        """
        # Replaces Swift's DiskFileRouter object reference with ours.
        self._diskfile_router = GlusterSwiftDiskFileRouter(conf, self.logger)
        self.devices = conf.get('devices', '/mnt/gluster-object')
        self.swift_dir = conf.get('swift_dir', '/etc/swift')
        self.object_ring = self.get_object_ring()

    def container_update(self, *args, **kwargs):
        """
        Update the container when objects are updated.

        For Gluster, this is just a no-op, since a container is just the
        directory holding all the objects (sub-directory hierarchy of files).
        """
        return

    def get_object_ring(self):
        return Ring(self.swift_dir, ring_name='object')

    def _create_expiring_tracker_object(self, object_path):
        try:

            # Check if gsexpiring volume is present in ring
            if not any(d.get('device', None) == self.expiring_objects_account
                       for d in self.object_ring.devs):
                raise Exception("%s volume not in ring" %
                                self.expiring_objects_account)

            # Check if gsexpiring is mounted.
            expiring_objects_account_path = \
                os.path.join(self.devices, self.expiring_objects_account)
            mount_check = self._diskfile_router['junk'].mount_check
            if mount_check and not do_ismount(expiring_objects_account_path):
                raise Exception("Path %s doesn't exist or is not a mount "
                                "point" % expiring_objects_account_path)

            # Create object directory
            object_dir = os.path.dirname(object_path)
            try:
                mkdirs(object_dir)
            except OSError as err:
                mkdirs(object_dir)  # handle race

            # Create zero-byte file
            try:
                os.mknod(object_path)
            except OSError as err:
                if err.errno != errno.EEXIST:
                    raise
        except Exception as e:
            self.logger.error("Creation of tracker object %s failed: %s" %
                              (object_path, str(e)))

    def async_update(self, op, account, container, obj, host, partition,
                     contdevice, headers_out, objdevice, policy):
        """
        In Openstack Swift, this method is called by:
            * container_update (a no-op in gluster-swift)
            * delete_at_update (to PUT objects into .expiring_objects account)

        The Swift's version of async_update only sends the request to
        container-server to PUT the object. The container-server calls
        container_update method which makes an entry for the object in it's
        database. No actual object is created on disk.

        But in gluster-swift container_update is a no-op, so we'll
        have to PUT an actual object. We override async_update to create a
        container first and then the corresponding "tracker object" which
        tracks expired objects scheduled for deletion.
        """
        object_path = os.path.join(self.devices, account, container, obj)

        threadpool = self._diskfile_router[policy].threadpools[objdevice]
        threadpool.run_in_thread(self._create_expiring_tracker_object,
                                 object_path)

    @public
    @timing_stats()
    def PUT(self, request):
        try:
            # now call swift's PUT method
            return server.ObjectController.PUT(self, request)
        except (AlreadyExistsAsFile, AlreadyExistsAsDir):
            device = \
                split_and_validate_path(request, 1, 5, True)
            return HTTPConflict(drive=device, request=request)

    @public
    @replication
    @timing_stats(sample_rate=0.1)
    def REPLICATE(self, request):
        """
        In Swift, this method handles REPLICATE requests for the Swift
        Object Server.  This is used by the object replicator to get hashes
        for directories.

        Gluster-Swift does not support this as it expects the underlying
        GlusterFS to take care of replication
        """
        return HTTPNotImplemented(request=request)

    @public
    @replication
    @timing_stats(sample_rate=0.1)
    def REPLICATION(self, request):
        return HTTPNotImplemented(request=request)


def app_factory(global_conf, **local_conf):
    """paste.deploy app factory for creating WSGI object server apps"""
    conf = global_conf.copy()
    conf.update(local_conf)
    return ObjectController(conf)
