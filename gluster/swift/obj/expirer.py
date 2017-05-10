# Copyright (c) 2016 Red Hat
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

# This import will monkey-patch Ring and other classes.
# Do not remove.
import gluster.swift.common.constraints  # noqa

import errno
import os

from gluster.swift.common.utils import delete_tracker_object

from swift.obj.expirer import ObjectExpirer as SwiftObjectExpirer
from swift.common.http import HTTP_NOT_FOUND
from swift.common.internal_client import InternalClient, UnexpectedResponse
from gluster.swift.common.utils import ThreadPool

EXCLUDE_DIRS = ('.trashcan', '.glusterfs')


class GlusterSwiftInternalClient(InternalClient):

    def __init__(self, conf_path, user_agent, request_tries,
                 allow_modify_pipeline=False, devices=None):
        super(GlusterSwiftInternalClient, self).__init__(
            conf_path, user_agent, request_tries, allow_modify_pipeline)
        self.devices = devices

    def get_account_info(self, account):
        # Supposed to return container count and object count in gsexpiring
        # account. This is used by object expirer only for logging.
        return (0, 0)

    def delete_container(self, account, container, acceptable_statuses=None):
        container_path = os.path.join(self.devices, account, container)
        try:
            os.rmdir(container_path)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise

    def iter_containers(self, account):
        account_path = os.path.join(self.devices, account)
        for container in os.listdir(account_path):
            if container in EXCLUDE_DIRS:
                continue
            container_path = os.path.join(account_path, container)
            if os.path.isdir(container_path):
                yield {'name': container.encode('utf8')}

    def iter_objects(self, account, container):
        container_path = os.path.join(self.devices, account, container)
        # TODO: Use a slightly better implementation of os.walk()
        for (root, dirs, files) in os.walk(container_path):
            for f in files:
                obj_path = os.path.join(root, f)
                obj = obj_path[(len(container_path) + 1):]
                yield {'name': obj.encode('utf8')}


class ObjectExpirer(SwiftObjectExpirer):

    def __init__(self, conf, logger=None, swift=None):

        conf_path = conf.get('__file__') or '/etc/swift/object-expirer.conf'
        self.devices = conf.get('devices', '/mnt/gluster-object')
        # Do not retry DELETEs on getting 404. Hence default is set to 1.
        request_tries = int(conf.get('request_tries') or 1)
        # Use our extended version of InternalClient
        swift = GlusterSwiftInternalClient(
            conf_path, 'Gluster Swift Object Expirer', request_tries,
            devices=self.devices)
        # Let the parent class initialize self.swift
        super(ObjectExpirer, self).__init__(conf, logger=logger, swift=swift)

        self.reseller_prefix = conf.get('reseller_prefix', 'AUTH').strip()
        if not self.reseller_prefix.endswith('_'):
            self.reseller_prefix = self.reseller_prefix + '_'

        # nthread=0 is intentional. This ensures that no green pool is
        # used. Call to force_run_in_thread() will ensure that the method
        # passed as arg is run in a real external thread using eventlet.tpool
        # which has a threadpool of 20 threads (default)
        self.threadpool = ThreadPool(nthreads=0)

    def pop_queue(self, container, obj):
        """
        In Swift, this method removes tracker object entry directly from
        container database. In gluster-swift, this method deletes tracker
        object directly from filesystem.
        """
        container_path = os.path.join(self.devices,
                                      self.expiring_objects_account,
                                      container)
        self.threadpool.force_run_in_thread(delete_tracker_object,
                                            container_path, obj)

    def delete_actual_object(self, actual_obj, timestamp):
        """
        Swift's expirer will re-attempt expiring if the source object is not
        available (404 or ANY other error) up to self.reclaim_age seconds
        before it gives up and deletes the entry in the queue.

        Don't do this in gluster-swift. GlusterFS isn't eventually consistent
        and has no concept of hand-off nodes. If actual data object doesn't
        exist (404), remove tracker object from the queue (filesystem).

        However if DELETE fails due a reason other than 404, do not remove
        tracker object yet, follow Swift's behaviour of waiting till
        self.reclaim_age seconds.

        This method is just a wrapper around parent class's method. All this
        wrapper does is ignore 404 failures.
        """
        try:
            super(ObjectExpirer, self).delete_actual_object(
                actual_obj, timestamp)
        except UnexpectedResponse as err:
            if err.resp.status_int != HTTP_NOT_FOUND:
                raise
