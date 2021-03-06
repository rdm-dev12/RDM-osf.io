# -*- coding: utf-8 -*-
'''
Common functions for timestamp.
'''
from __future__ import absolute_import
import datetime
import hashlib
import logging
import os
import shutil
import subprocess
import time
import traceback

from urllib3.util.retry import Retry
import requests
import pytz

from api.base import settings as api_settings
from api.base.utils import waterbutler_api_url_for
from django.utils import timezone
from osf.models import (
    AbstractNode, BaseFileNode, Guid, RdmFileTimestamptokenVerifyResult, RdmUserKey,
    OSFUser
)
from website import util
from website import settings
from website.util import waterbutler

logger = logging.getLogger(__name__)

RESULT_MESSAGE = {
    api_settings.TIME_STAMP_TOKEN_CHECK_NG:
        api_settings.TIME_STAMP_TOKEN_CHECK_NG_MSG,  # 'NG'
    api_settings.TIME_STAMP_TOKEN_NO_DATA:
        api_settings.TIME_STAMP_TOKEN_NO_DATA_MSG,  # 'TST missing(Retrieving Failed)'
    api_settings.TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND:
        api_settings.TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND_MSG,  # 'TST missing(Unverify)'
    api_settings.FILE_NOT_EXISTS:
        api_settings.FILE_NOT_EXISTS_MSG  # 'FILE missing'
}

def get_error_list(pid):
    '''
    Retrieve from the database the list of all timestamps that has an error.
    '''
    data_list = RdmFileTimestamptokenVerifyResult.objects.filter(project_id=pid).order_by('provider', 'path')
    provider_error_list = []
    provider = None
    error_list = []

    for data in data_list:
        if data.inspection_result_status == api_settings.TIME_STAMP_TOKEN_CHECK_SUCCESS:
            continue

        if not provider:
            provider = data.provider
        elif provider != data.provider:
            provider_error_list.append({'provider': provider, 'error_list': error_list})
            provider = data.provider
            error_list = []

        if data.inspection_result_status in RESULT_MESSAGE:
            verify_result_title = RESULT_MESSAGE[data.inspection_result_status]
        else:  # 'FILE missing(Unverify)'
            verify_result_title = \
                api_settings.FILE_NOT_EXISTS_TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND_MSG

        # User and date of the verification
        verify_user = OSFUser.objects.get(id=data.verify_user)
        verify_date = data.verify_date.strftime('%Y/%m/%d %H:%M:%S')

        # Get file info
        base_file_data = BaseFileNode.objects.filter(_id=data.file_id)
        base_file_data_exists = base_file_data.exists()
        file_versions = None
        if base_file_data_exists:
            base_file_data = base_file_data.get()
            file_versions = base_file_data.versions.all()

        # Get creator info
        creator = None
        if data.upload_file_modified_user is not None:
            creator = OSFUser.objects.get(id=data.upload_file_modified_user)
        elif data.upload_file_created_user is not None:
            creator = OSFUser.objects.get(id=data.upload_file_created_user)
        elif file_versions is not None and file_versions.exists():
            creator = file_versions.latest('id').creator

        # Change None to '' (empty string)
        data.path = '' if data.path is None else data.path
        data.upload_file_created_at = '' if data.upload_file_created_at is None else \
            data.upload_file_created_at
        data.verify_file_created_at = '' if data.verify_file_created_at is None else \
            data.verify_file_created_at
        data.upload_file_modified_at = '' if data.upload_file_modified_at is None else \
            data.upload_file_modified_at
        data.verify_file_modified_at = '' if data.verify_file_modified_at is None else \
            data.verify_file_modified_at
        data.upload_file_size = '' if data.upload_file_size is None else \
            data.upload_file_size
        data.verify_file_size = '' if data.verify_file_size is None else \
            data.verify_file_size

        # Generate error_info dictionary
        error_info = {
            'creator_name': '',
            'creator_email': '',
            'creator_id': '',
            'file_path': data.path,
            'file_id': data.file_id,
            'file_create_date_on_upload': data.upload_file_created_at,
            'file_create_date_on_verify': data.verify_file_created_at,
            'file_modify_date_on_upload': data.upload_file_modified_at,
            'file_modify_date_on_verify': data.verify_file_modified_at,
            'file_size_on_upload': data.upload_file_size,
            'file_size_on_verify': data.verify_file_size,
            'file_version': '',
            'project_id': data.project_id,
            'organization_id': '',
            'organization_name': '',
            'verify_user_id': verify_user._id,
            'verify_user_name': verify_user.fullname,
            'verify_date': verify_date,
            'verify_result_title': verify_result_title,
        }

        if base_file_data_exists and provider == 'osfstorage':
            error_info['file_version'] = base_file_data.current_version_number

        if creator is not None:
            error_info['creator_name'] = creator.fullname
            error_info['creator_email'] = creator.username
            error_info['creator_id'] = creator._id

            institution = creator.affiliated_institutions.first()
            if institution is not None:
                error_info['organization_id'] = institution._id
                error_info['organization_name'] = institution.name

        error_list.append(error_info)

    if error_list:
        provider_error_list.append({'provider': provider, 'error_list': error_list})

    return provider_error_list

def get_full_list(uid, pid, node):
    '''
    Get a full list of timestamps from all files uploaded to a storage.
    '''
    user_info = OSFUser.objects.get(id=uid)
    cookie = user_info.get_or_create_cookie()

    api_url = util.api_v2_url('nodes/{}/files'.format(pid))
    headers = {'content-type': 'application/json'}
    cookies = {settings.COOKIE_NAME: cookie}

    file_res = requests.get(api_url, headers=headers, cookies=cookies)
    provider_json_res = file_res.json()
    file_res.close()
    provider_list = []

    for provider_data in provider_json_res['data']:
        waterbutler_meta_url = waterbutler_api_url_for(
            pid,
            provider_data['attributes']['provider'],
            '/',
            meta=int(time.mktime(datetime.datetime.now().timetuple()))
        )
        waterbutler_json_res = None
        waterbutler_res = requests.get(waterbutler_meta_url, headers=headers, cookies=cookies)
        waterbutler_json_res = waterbutler_res.json()
        waterbutler_res.close()

        file_list = []
        child_file_list = []
        for file_data in waterbutler_json_res['data']:
            if file_data['attributes']['kind'] == 'folder':
                child_file_list.extend(
                    waterbutler_folder_file_info(
                        pid,
                        provider_data['attributes']['provider'],
                        file_data['attributes']['path'],
                        node, cookies, headers
                    )
                )
            else:
                file_info = None
                basefile_node = BaseFileNode.resolve_class(
                    provider_data['attributes']['provider'],
                    BaseFileNode.FILE
                ).get_or_create(
                    node,
                    file_data['attributes']['path']
                )
                basefile_node.save()
                file_info = {
                    'file_id': basefile_node._id,
                    'file_name': file_data['attributes'].get('name'),
                    'file_path': file_data['attributes'].get('materialized'),
                    'size': file_data['attributes'].get('size'),
                    'created': file_data['attributes'].get('created_utc'),
                    'modified': file_data['attributes'].get('modified_utc'),
                    'file_version': ''
                }
                if provider_data['attributes']['provider'] == 'osfstorage':
                    file_info['file_version'] = file_data['attributes']['extra'].get('version')
                if file_info:
                    file_list.append(file_info)

        file_list.extend(child_file_list)

        if file_list:
            provider_files = {
                'provider': provider_data['attributes']['provider'],
                'provider_file_list': file_list
            }
            provider_list.append(provider_files)

    return provider_list

def check_file_timestamp(uid, node, data):
    user = OSFUser.objects.get(id=uid)
    cookie = user.get_or_create_cookie()
    tmp_dir = None
    result = None

    try:
        file_node = BaseFileNode.objects.get(_id=data['file_id'])
        current_datetime = datetime.datetime.now(pytz.timezone('Asia/Tokyo'))
        current_datetime_str = current_datetime.strftime('%Y%m%d%H%M%S%f')
        tmp_dir = 'tmp_{}_{}_{}'.format(user._id, file_node._id, current_datetime_str)

        if not os.path.exists(tmp_dir):
            os.mkdir(tmp_dir)

        download_file_path = waterbutler.download_file(cookie, file_node, tmp_dir)

        if not userkey_generation_check(user._id):
            userkey_generation(user._id)

        verify_check = TimeStampTokenVerifyCheck()
        result = verify_check.timestamp_check(
            user._id, data, node._id, download_file_path, tmp_dir
        )

        shutil.rmtree(tmp_dir)
        return result

    except Exception as err:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        logger.exception(err)
        raise

#@app.task(bind=True)
def do_verification(uid,pid,node):
 #   self.update_state(state="PROGRESS", meta={'progress':90})
 #   self.update_state(state="PROGRESS", meta={'progress': 100})
    try:
        for provider_dict in  get_full_list(uid,pid,node):
        	for p_item in provider_dict['provider_file_list']:
			p_item['provider']=provider_dict['provider']
			print(check_file_timestamp(uid,node,p_item))
    except Exception as err:
	print(err)
	logger.exception(err)
	raise


def on_raw_message(body):
    print(body)

def add_token(uid, node, data):
    user = OSFUser.objects.get(id=uid)
    cookie = user.get_or_create_cookie()
    tmp_dir = None

    try:
        file_node = BaseFileNode.resolve_class(
            data['provider'], BaseFileNode.FILE).get_or_create(node, data['file_path'])
        file_node.save()
        data['file_id'] = file_node._id

        # Request To Download File
        tmp_dir = 'tmp_{}'.format(user._id)
        count = 1
        while os.path.exists(tmp_dir):
            count += 1
            tmp_dir = 'tmp_{}_{}'.format(user._id, count)
        os.mkdir(tmp_dir)
        download_file_path = waterbutler.download_file(cookie, file_node, tmp_dir)

        if not userkey_generation_check(user._id):
            userkey_generation(user._id)

        addTimestamp = AddTimestamp()
        result = addTimestamp.add_timestamp(
            user._id, data, node._id, download_file_path, tmp_dir
        )

        shutil.rmtree(tmp_dir)
        return result

    except Exception as err:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        logger.exception(err)
        raise

def file_created_or_updated(node, payload, user_id, created_flag):
    file_node = BaseFileNode.resolve_class(
        payload['metadata']['provider'], BaseFileNode.FILE
    ).get_or_create(node, payload['metadata'].get('materialized'))
    file_node.save()
    created_at = payload['metadata'].get('created_utc')
    modified_at = payload['metadata'].get('modified_utc')
    version = ''
    if not created_at:
        created_at = None
    if not modified_at:
        modified_at = None
    if payload['metadata']['provider'] == 'osf_storage':
        version = payload['metadata']['extra'].get('version')
    file_info = {
        'file_id': file_node._id,
        'file_name': payload['metadata'].get('name'),
        'file_path': payload['metadata'].get('materialized'),
        'size': payload['metadata'].get('size'),
        'created': created_at,
        'modified': modified_at,
        'version': version,
        'provider': payload['metadata'].get('provider')
    }
    add_token(user_id, node, file_info)

    # Update created/modified user in timestamp result
    verify_data = RdmFileTimestamptokenVerifyResult.objects.filter(
        file_id=file_info['file_id']).first()
    if verify_data:
        if created_flag:
            verify_data.upload_file_created_user = user_id
        else:  # Updated
            verify_data.upload_file_modified_user = user_id
        verify_data.upload_file_created_at = file_info['created']
        verify_data.upload_file_modified_at = file_info['modified']
        verify_data.upload_file_size = file_info['size']
        verify_data.save()

def waterbutler_folder_file_info(pid, provider, path, node, cookies, headers):
    # get waterbutler folder file
    if provider == 'osfstorage':
        waterbutler_meta_url = waterbutler_api_url_for(
            pid, provider,
            '/' + path,
            meta=int(time.mktime(datetime.datetime.now().timetuple()))
        )
    else:
        waterbutler_meta_url = waterbutler_api_url_for(
            pid, provider,
            path,
            meta=int(time.mktime(datetime.datetime.now().timetuple()))
        )

    waterbutler_res = requests.get(waterbutler_meta_url, headers=headers, cookies=cookies)
    waterbutler_json_res = waterbutler_res.json()
    waterbutler_res.close()
    file_list = []
    child_file_list = []
    for file_data in waterbutler_json_res['data']:
        if file_data['attributes']['kind'] == 'folder':
            child_file_list.extend(waterbutler_folder_file_info(
                pid, provider, file_data['attributes']['path'],
                node, cookies, headers))
        else:
            basefile_node = BaseFileNode.resolve_class(
                provider,
                BaseFileNode.FILE
            ).get_or_create(
                node,
                file_data['attributes']['path']
            )
            basefile_node.save()
            if provider == 'osfstorage':
                file_info = {
                    'file_name': file_data['attributes']['name'],
                    'file_path': file_data['attributes']['materialized'],
                    'file_kind': file_data['attributes']['kind'],
                    'file_id': basefile_node._id,
                    'version': file_data['attributes']['extra']['version']
                }
            else:
                file_info = {
                    'file_name': file_data['attributes']['name'],
                    'file_path': file_data['attributes']['materialized'],
                    'file_kind': file_data['attributes']['kind'],
                    'file_id': basefile_node._id,
                    'version': ''
                }

            file_list.append(file_info)

    file_list.extend(child_file_list)
    return file_list

def userkey_generation_check(guid):
    return RdmUserKey.objects.filter(guid=Guid.objects.get(_id=guid).object_id).exists()

def userkey_generation(guid):
    logger.info('userkey_generation guid:' + guid)

    try:
        generation_date = datetime.datetime.now()
        generation_date_str = generation_date.strftime('%Y%m%d%H%M%S')
        generation_date_hash = hashlib.md5(generation_date_str).hexdigest()
        generation_pvt_key_name = api_settings.KEY_NAME_FORMAT.format(
            guid, generation_date_hash, api_settings.KEY_NAME_PRIVATE, api_settings.KEY_EXTENSION)
        generation_pub_key_name = api_settings.KEY_NAME_FORMAT.format(
            guid, generation_date_hash, api_settings.KEY_NAME_PUBLIC, api_settings.KEY_EXTENSION)
        # private key generation
        pvt_key_generation_cmd = [
            api_settings.OPENSSL_MAIN_CMD, api_settings.OPENSSL_OPTION_GENRSA,
            api_settings.OPENSSL_OPTION_OUT,
            os.path.join(api_settings.KEY_SAVE_PATH, generation_pvt_key_name),
            api_settings.KEY_BIT_VALUE
        ]

        pub_key_generation_cmd = [
            api_settings.OPENSSL_MAIN_CMD, api_settings.OPENSSL_OPTION_RSA,
            api_settings.OPENSSL_OPTION_IN,
            os.path.join(api_settings.KEY_SAVE_PATH, generation_pvt_key_name),
            api_settings.OPENSSL_OPTION_PUBOUT, api_settings.OPENSSL_OPTION_OUT,
            os.path.join(api_settings.KEY_SAVE_PATH, generation_pub_key_name)
        ]

        prc = subprocess.Popen(
            pvt_key_generation_cmd, shell=False, stdin=subprocess.PIPE,
            stderr=subprocess.PIPE, stdout=subprocess.PIPE)

        stdout_data, stderr_data = prc.communicate()

        prc = subprocess.Popen(
            pub_key_generation_cmd, shell=False, stdin=subprocess.PIPE,
            stderr=subprocess.PIPE, stdout=subprocess.PIPE)

        stdout_data, stderr_data = prc.communicate()

        pvt_userkey_info = create_rdmuserkey_info(
            Guid.objects.get(_id=guid).object_id, generation_pvt_key_name,
            api_settings.PRIVATE_KEY_VALUE, generation_date)

        pub_userkey_info = create_rdmuserkey_info(
            Guid.objects.get(_id=guid).object_id, generation_pub_key_name,
            api_settings.PUBLIC_KEY_VALUE, generation_date)

        pvt_userkey_info.save()
        pub_userkey_info.save()

    except Exception as error:
        logger.exception(error)
        raise

def create_rdmuserkey_info(user_id, key_name, key_kind, date):
    userkey_info = RdmUserKey()
    userkey_info.guid = user_id
    userkey_info.key_name = key_name
    userkey_info.key_kind = key_kind
    userkey_info.created_time = date
    return userkey_info


class AddTimestamp:
    #1 create tsq (timestamp request) from file, and keyinfo
    def get_timestamp_request(self, file_name):
        cmd = [
            api_settings.OPENSSL_MAIN_CMD, api_settings.OPENSSL_OPTION_TS,
            api_settings.OPENSSL_OPTION_QUERY, api_settings.OPENSSL_OPTION_DATA,
            file_name, api_settings.OPENSSL_OPTION_CERT, api_settings.OPENSSL_OPTION_SHA512
        ]
        process = subprocess.Popen(
            cmd, shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        stdout_data, stderr_data = process.communicate()
        return stdout_data

    #2 send tsq to TSA, and recieve tsr (timestamp token)
    def get_timestamp_response(self, file_name, ts_request_file, key_file):
        res_content = None
        try:
            retries = Retry(
                total=api_settings.REQUEST_TIME_OUT, backoff_factor=1,
                status_forcelist=api_settings.ERROR_HTTP_STATUS)
            session = requests.Session()
            session.mount('http://', requests.adapters.HTTPAdapter(max_retries=retries))
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=retries))

            res = requests.post(
                api_settings.TIME_STAMP_AUTHORITY_URL, headers=api_settings.REQUEST_HEADER,
                data=ts_request_file, stream=True)
            res_content = res.content
            res.close()

        except Exception as ex:
            logger.exception(ex)
            traceback.print_exc()
            res_content = None

        return res_content

    def add_timestamp(self, guid, file_info, project_id, file_name, tmp_dir):
        user_id = Guid.objects.get(_id=guid).object_id

        key_file_name = RdmUserKey.objects.get(
            guid=user_id, key_kind=api_settings.PUBLIC_KEY_VALUE
        ).key_name

        tsa_response = self.get_timestamp_response(
            file_name, self.get_timestamp_request(file_name), key_file_name
        )

        verify_data = RdmFileTimestamptokenVerifyResult.objects.filter(
            file_id=file_info['file_id'])
        if verify_data.exists():
            verify_data = verify_data.get()
        else:
            verify_data = RdmFileTimestamptokenVerifyResult()
            verify_data.file_id = file_info['file_id']
            verify_data.project_id = project_id
            verify_data.provider = file_info['provider']
            verify_data.path = file_info['file_path']
            verify_data.inspection_result_status = api_settings.TIME_STAMP_TOKEN_UNCHECKED

        verify_data.key_file_name = key_file_name
        verify_data.timestamp_token = tsa_response
        verify_data.save()

        return TimeStampTokenVerifyCheck().timestamp_check(
            guid, file_info, project_id, file_name, tmp_dir)


class TimeStampTokenVerifyCheck:
    # get abstractNode
    def get_abstractNode(self, node_id):
        # get project name
        try:
            abstractNode = AbstractNode.objects.get(id=node_id)
        except Exception as err:
            logging.exception(err)
            abstractNode = None

        return abstractNode

    # get verify result
    def get_verifyResult(self, file_id, project_id, provider, path):
        try:
            if RdmFileTimestamptokenVerifyResult.objects.filter(file_id=file_id).exists():
                verifyResult = RdmFileTimestamptokenVerifyResult.objects.get(file_id=file_id)
            else:
                verifyResult = None

        except Exception as err:
            logging.exception(err)
            verifyResult = None

        return verifyResult

    # get baseFileNode
    def get_baseFileNode(self, file_id):
        try:
            baseFileNode = BaseFileNode.objects.get(_id=file_id)
        except Exception as err:
            logging.exception(err)
            baseFileNode = None

        return baseFileNode

    # get baseFileNode filepath
    def get_filenameStruct(self, fsnode, fname):
        try:
            if fsnode.parent is not None:
                fname = self.get_filenameStruct(fsnode.parent, fname) + '/' + fsnode.name
            else:
                fname = fsnode.name
        except Exception as err:
            logging.exception(err)

        return fname

    def create_rdm_filetimestamptokenverify(
            self, file_id, project_id, provider, path, inspection_result_status, userid):

        userKey = RdmUserKey.objects.get(guid=userid, key_kind=api_settings.PUBLIC_KEY_VALUE)
        create_data = RdmFileTimestamptokenVerifyResult()
        create_data.file_id = file_id
        create_data.project_id = project_id
        create_data.provider = provider
        create_data.key_file_name = userKey.key_name
        create_data.path = path
        create_data.inspection_result_status = inspection_result_status
        create_data.verify_user = userid
        create_data.verify_date = timezone.now()
        return create_data

    # timestamp token check
    def timestamp_check(self, guid, file_info, project_id, file_name, tmp_dir):
        userid = Guid.objects.get(_id=guid).object_id
        file_id = file_info['file_id']
        provider = file_info['provider']
        path = file_info['file_path']

        # get verify result
        verify_result = self.get_verifyResult(file_id, project_id, provider, path)

        ret = 0
        verify_result_title = None

        try:
            # get file information, verifyresult table
            if provider == 'osfstorage':
                # 'osfstorage'
                baseFileNode = self.get_baseFileNode(file_id)
                if baseFileNode.is_deleted and not verify_result:
                    # if file was deleted ,and verify result does not exist:
                    # update verifyResult:'FILE missing'
                    ret = api_settings.FILE_NOT_EXISTS
                    verify_result_title = api_settings.FILE_NOT_EXISTS_MSG  # 'FILE missing'
                    verify_result = self.create_rdm_filetimestamptokenverify(
                        file_id, project_id, provider, path, ret, userid)

                elif baseFileNode.is_deleted and verify_result and not verify_result.timestamp_token:
                    # if file does not exist ,and verify result does not exist in db:
                    # update verifyResult 'FILE missing(Unverify)'
                    verify_result.inspection_result_status = \
                        api_settings.FILE_NOT_EXISTS_TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND
                    ret = api_settings.FILE_NOT_EXISTS_TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND
                    verify_result_title = \
                        api_settings.FILE_NOT_EXISTS_TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND_MSG

                elif baseFileNode.is_deleted and verify_result:
                    # if file was deleted, and verify result exists in db:
                    # update verifyResult 'FILE missing(Unverify)'
                    verify_result.inspection_result_status = \
                        api_settings.FILE_NOT_EXISTS_TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND
                    ret = api_settings.FILE_NOT_EXISTS_TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND_MSG

                elif not baseFileNode.is_deleted and not verify_result:
                    # if file was deleted, and verify result does not exist in db:
                    # update verifyResult 'TST missing(Unverify)'
                    ret = api_settings.TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND
                    verify_result_title = \
                        api_settings.TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND_MSG
                    verify_result = self.create_rdm_filetimestamptokenverify(
                        file_id, project_id, provider, path, ret, userid)

                elif not baseFileNode.is_deleted and not verify_result.timestamp_token:
                    # if file exists and  verifyResult.timestamp_token does not exist:
                    # update verifyResult 'TST missing(Retrieving Failed)'
                    verify_result.inspection_result_status = api_settings.TIME_STAMP_TOKEN_NO_DATA
                    ret = api_settings.TIME_STAMP_TOKEN_NO_DATA
                    verify_result_title = api_settings.TIME_STAMP_TOKEN_NO_DATA_MSG

            else:
                # storage other than osfstorage:
                if not verify_result:
                    # if file does not exist, and  verify result does not exist:
                    # update verifyResult 'TST missing(Unverify)'
                    ret = api_settings.TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND
                    verify_result_title = api_settings.TIME_STAMP_TOKEN_CHECK_FILE_NOT_FOUND_MSG
                    verify_result = self.create_rdm_filetimestamptokenverify(
                        file_id, project_id, provider, path, ret, userid)

                elif not verify_result.timestamp_token:
                    # if timestamptoken does not exist:
                    # update verifyResult 'TST missing(Retrieving Failed)'
                    verify_result.inspection_result_status = api_settings.TIME_STAMP_TOKEN_NO_DATA
                    ret = api_settings.TIME_STAMP_TOKEN_NO_DATA
                    verify_result_title = api_settings.TIME_STAMP_TOKEN_NO_DATA_MSG

            if ret == 0:
                timestamptoken_file = guid + '.tsr'
                timestamptoken_file_path = os.path.join(tmp_dir, timestamptoken_file)
                try:
                    with open(timestamptoken_file_path, 'wb') as fout:
                        fout.write(verify_result.timestamp_token)

                except Exception as err:
                    raise err

                # verify timestamptoken and rootCA
                cmd = [
                    api_settings.OPENSSL_MAIN_CMD, api_settings.OPENSSL_OPTION_TS,
                    api_settings.OPENSSL_OPTION_VERIFY, api_settings.OPENSSL_OPTION_DATA,
                    file_name, api_settings.OPENSSL_OPTION_IN, timestamptoken_file_path,
                    api_settings.OPENSSL_OPTION_CAFILE,
                    os.path.join(api_settings.KEY_SAVE_PATH, api_settings.VERIFY_ROOT_CERTIFICATE)
                ]
                prc = subprocess.Popen(
                    cmd, shell=False, stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE, stdout=subprocess.PIPE)
                stdout_data, stderr_data = prc.communicate()
                ret = api_settings.TIME_STAMP_TOKEN_UNCHECKED

                if stdout_data.__str__().find(api_settings.OPENSSL_VERIFY_RESULT_OK) > -1:
                    ret = api_settings.TIME_STAMP_TOKEN_CHECK_SUCCESS
                    verify_result_title = api_settings.TIME_STAMP_TOKEN_CHECK_SUCCESS_MSG  # 'OK'

                else:
                    ret = api_settings.TIME_STAMP_TOKEN_CHECK_NG
                    verify_result_title = api_settings.TIME_STAMP_TOKEN_CHECK_NG_MSG  # 'NG'

                verify_result.inspection_result_status = ret

            file_created_at = file_info.get('created')
            file_modified_at = file_info.get('modified')
            file_size = file_info.get('size')

            if not file_created_at:
                file_created_at = None
            if not file_modified_at:
                file_modified_at = None
            if not file_size:
                file_size = None

            verify_result.verify_date = datetime.datetime.now()
            verify_result.verify_user = userid
            verify_result.verify_file_created_at = file_created_at
            verify_result.verify_file_modified_at = file_modified_at
            verify_result.verify_file_size = file_size
            verify_result.save()
        except Exception as err:
            logging.exception(err)

        # RDMINFO: TimeStampVerify
        if provider == 'osfstorage':
            if not baseFileNode._path:
                filename = self.get_filenameStruct(baseFileNode, '')
            else:
                filename = baseFileNode._path
            filepath = baseFileNode.provider + filename
        else:
            filepath = provider + path

        return {
            'verify_result': ret,
            'verify_result_title': verify_result_title,
            'filepath': filepath
        }
