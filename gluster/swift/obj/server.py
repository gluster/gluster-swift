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

from swift.common.swob import HTTPConflict, HTTPNotImplemented
from swift.common.utils import public, timing_stats, replication
from gluster.swift.common.exceptions import AlreadyExistsAsFile, \
    AlreadyExistsAsDir
from swift.common.request_helpers import split_and_validate_path

from swift.obj import server

from gluster.swift.obj.diskfile import DiskFileManager


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

    def container_update(self, *args, **kwargs):
        """
        Update the container when objects are updated.

        For Gluster, this is just a no-op, since a container is just the
        directory holding all the objects (sub-directory hierarchy of files).
        """
        return

    def delete_at_update(self, *args, **kwargs):
        """
        Update the expiring objects container when objects are updated.

        For Gluster, this is a no-op as there are no container DB entries
        to be created that tracks objects to be expired. Objects to be
        expired will be determined by crawling the filesystem directly.
        """
        return

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
