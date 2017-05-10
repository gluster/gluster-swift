# Copyright (c) 2012-2013 Red Hat, Inc.
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

""" Container Server for Gluster Swift UFO """

# Simply importing this monkey patches the constraint handling to fit our
# needs
import gluster.swift.common.constraints    # noqa

from swift.container import server
from gluster.swift.common.DiskDir import DiskDir
from swift.common.utils import public, timing_stats, config_true_value
from swift.common.exceptions import DiskFileNoSpace
from swift.common.swob import HTTPInsufficientStorage, HTTPNotFound, \
    HTTPPreconditionFailed
from swift.common.request_helpers import get_param, get_listing_content_type, \
    split_and_validate_path
from swift.common.constraints import check_mount
from swift.container.server import gen_resp_headers
from swift.common import constraints


class ContainerController(server.ContainerController):
    """
    Subclass of the container server's ContainerController which replaces the
    _get_container_broker() method so that we can use Gluster's DiskDir
    duck-type of the container DatabaseBroker object, and make the
    account_update() method a no-op (information is simply stored on disk and
    already updated by virtue of performaing the file system operations
    directly).
    """

    def _get_container_broker(self, drive, part, account, container, **kwargs):
        """
        Overriden to provide the GlusterFS specific broker that talks to
        Gluster for the information related to servicing a given request
        instead of talking to a database.

        :param drive: drive that holds the container
        :param part: partition the container is in
        :param account: account name
        :param container: container name
        :returns: DiskDir object, a duck-type of DatabaseBroker
        """
        return DiskDir(self.root, drive, account, container, self.logger,
                       **kwargs)

    def account_update(self, req, account, container, broker):
        """
        Update the account server(s) with latest container info.

        For Gluster, this is just a no-op, since an account is just the
        directory holding all the container directories.

        :param req: swob.Request object
        :param account: account name
        :param container: container name
        :param broker: container DB broker object
        :returns: None.
        """
        return None

    @public
    @timing_stats()
    def PUT(self, req):
        try:
            return server.ContainerController.PUT(self, req)
        except DiskFileNoSpace:
            # As container=directory in gluster-swift, we might run out of
            # space or exceed quota when creating containers.
            drive = req.split_path(1, 1, True)
            return HTTPInsufficientStorage(drive=drive, request=req)

    @public
    @timing_stats()
    def GET(self, req):
        """
        Handle HTTP GET request.

        This method is exact copy of swift.container.server.GET() except
        that this version of it passes 'out_content_type' information to
        broker.list_objects_iter()
        """
        drive, part, account, container, obj = split_and_validate_path(
            req, 4, 5, True)
        path = get_param(req, 'path')
        prefix = get_param(req, 'prefix')
        delimiter = get_param(req, 'delimiter')
        if delimiter and (len(delimiter) > 1 or ord(delimiter) > 254):
            # delimiters can be made more flexible later
            return HTTPPreconditionFailed(body='Bad delimiter')
        marker = get_param(req, 'marker', '')
        end_marker = get_param(req, 'end_marker')
        limit = constraints.CONTAINER_LISTING_LIMIT
        given_limit = get_param(req, 'limit')
        reverse = config_true_value(get_param(req, 'reverse'))

        if given_limit and given_limit.isdigit():
            limit = int(given_limit)
            if limit > constraints.CONTAINER_LISTING_LIMIT:
                return HTTPPreconditionFailed(
                    request=req,
                    body='Maximum limit is %d'
                    % constraints.CONTAINER_LISTING_LIMIT)
        out_content_type = get_listing_content_type(req)
        if self.mount_check and not check_mount(self.root, drive):
            return HTTPInsufficientStorage(drive=drive, request=req)
        broker = self._get_container_broker(drive, part, account, container,
                                            pending_timeout=0.1,
                                            stale_reads_ok=True)
        info, is_deleted = broker.get_info_is_deleted()
        resp_headers = gen_resp_headers(info, is_deleted=is_deleted)
        if is_deleted:
            return HTTPNotFound(request=req, headers=resp_headers)
        container_list = broker.list_objects_iter(
            limit, marker, end_marker, prefix, delimiter, path,
            storage_policy_index=info['storage_policy_index'],
            out_content_type=out_content_type, reverse=reverse)
        return self.create_listing(req, out_content_type, info, resp_headers,
                                   broker.metadata, container_list, container)


def app_factory(global_conf, **local_conf):
    """paste.deploy app factory for creating WSGI container server apps."""
    conf = global_conf.copy()
    conf.update(local_conf)
    return ContainerController(conf)
