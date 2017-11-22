# Copyright (c) 2016 Red Hat, Inc.
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

from swift.account.utils import FakeAccountBroker, get_response_headers
from swift.common.swob import HTTPOk, HTTPNoContent
from swift.common.utils import json, Timestamp
from xml.sax import saxutils


def account_listing_response(account, req, response_content_type, broker=None,
                             limit='', marker='', end_marker='', prefix='',
                             delimiter='', reverse=False):
    """
    This is an exact copy of swift.account.utis.account_listing_response()
    except for one difference i.e this method passes response_content_type
    to broker.list_containers_iter() method.
    """
    if broker is None:
        broker = FakeAccountBroker()

    resp_headers = get_response_headers(broker)

    account_list = broker.list_containers_iter(limit, marker, end_marker,
                                               prefix, delimiter,
                                               response_content_type, reverse)
    if response_content_type == 'application/json':
        data = []
        for (name, object_count, bytes_used, put_tstamp,
             is_subdir) in account_list:
            if is_subdir:
                data.append({'subdir': name})
            else:
                data.append({'name': name, 'count': object_count,
                             'bytes': bytes_used,
                             'last_modified': Timestamp(put_tstamp).isoformat})
        account_list = json.dumps(data)
    elif response_content_type.endswith('/xml'):
        output_list = ['<?xml version="1.0" encoding="UTF-8"?>',
                       '<account name=%s>' % saxutils.quoteattr(account)]
        for (name, object_count, bytes_used, put_tstamp,
             is_subdir) in account_list:
            if is_subdir:
                output_list.append(
                    '<subdir name=%s />' % saxutils.quoteattr(name))
            else:
                item = '<container><name>%s</name><count>%s</count>' \
                       '<bytes>%s</bytes><last_modified>%s</last_modified> \
                        </container>' % \
                       (saxutils.escape(name), object_count, bytes_used,
                        Timestamp(put_tstamp).isoformat)
                output_list.append(item)
        output_list.append('</account>')
        account_list = '\n'.join(output_list)
    else:
        if not account_list:
            resp = HTTPNoContent(request=req, headers=resp_headers)
            resp.content_type = response_content_type
            resp.charset = 'utf-8'
            return resp
        account_list = '\n'.join(r[0] for r in account_list) + '\n'
    ret = HTTPOk(body=account_list, request=req, headers=resp_headers)
    ret.content_type = response_content_type
    ret.charset = 'utf-8'
    return ret
