# -*- coding: utf-8 -*-
"""
Copyright (c) 2020 beyond-blockchain.org.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import bbc1
import binascii
import datetime
import hashlib
import json
import os
import string
import sys
import time
import xml.etree.ElementTree as ET

from bbc1.core import bbc_app, bbclib, bbc_config
from bbc1.core.bbc_error import *
from bbc1.core.ethereum import bbc_ethereum
from bbc1.core.message_key_types import KeyType
from bbc1.core.subsystem_tool_lib import wait_check_result_msg_type

from bbc1.lib import id_lib, registry_lib
from bbc1.lib.app_support_lib import Database, TransactionLabel
from bbc1.lib.app_support_lib import get_timestamp_in_seconds

from brownie import *
from flask import Blueprint, request, abort, jsonify, g


NAME_OF_DB = 'certify_db'


certify_user_table_definition = [
    ["user_id", "BLOB"],
    ["name", "TEXT"],
    ["public_key", "BLOB"],
    ["private_key", "BLOB"],
]

IDX_USER_ID = 0
IDX_NAME    = 1
IDX_PUBKEY  = 2
IDX_PRIVKEY = 3

# As a matter of convenience, we need two users: the registry and its user.
NAME_REGISTRY = 'registry'
NAME_USER     = 'user'


domain_id = bbclib.get_new_id("certify_web_domain", include_timestamp=False)


class User:

    def __init__(self, user_id, name, keypair):
        self.user_id = user_id
        self.name = name
        self.keypair = keypair


    @staticmethod
    def from_row(row):
        return User(
            row[IDX_USER_ID],
            row[IDX_NAME],
            bbclib.KeyPair(privkey=row[IDX_PRIVKEY], pubkey=row[IDX_PUBKEY])
        )


class Store:

    def __init__(self):
        self.db = Database()
        self.db.setup_db(domain_id, NAME_OF_DB)


    def close(self):
        try:
            self.db.close_db(domain_id, NAME_OF_DB)
        except KeyError:
            pass


    def read_user(self, name):
        rows = self.db.exec_sql(
            domain_id,
            NAME_OF_DB,
            'select * from user_table where name=?',
            name
        )
        if len(rows) <= 0:
            return None
        return User.from_row(rows[0])


    def setup(self):
        self.db.create_table_in_db(domain_id, NAME_OF_DB, 'user_table',
                certify_user_table_definition, primary_key=IDX_USER_ID,
                indices=[IDX_NAME])


    def write_user(self, user):
        self.db.exec_sql(
            domain_id,
            NAME_OF_DB,
            'insert into user_table values (?, ?, ?, ?)',
            user.user_id,
            user.name,
            user.keypair.public_key,
            user.keypair.private_key
        )


def abort_by_bad_content_type(content_type):
    abort(400, {
        'code': 'Bad Request',
        'message': 'Content-Type {0} is not expected'.format(content_type)
    })


def abort_by_merkle_root_not_found():
    abort(404, {
        'code': 'Not Found',
        'message': 'Merkle root not stored'
    })


def abort_by_subsystem_not_supported():
    abort(400, {
        'code': 'Bad Request',
        'message': 'non-supported subsystem'
    })


def abort_by_missing_param(param):
    abort(400, {
        'code': 'Bad Request',
        'message': '{0} is missing'.format(param)
    })


def dict2xml(dic):

    root = ET.fromstring('<c/>')
    dict2xml_element(root, dic)

    return root


def dict2xml_element(element, value):

    if isinstance(value, dict):
        for k, v in value.items():
            if k == 'proof':
                continue
            e = ET.SubElement(element, k, {'container': 'true'})
            dict2xml_element(e, v)

    elif isinstance(value, list):
        for v in value:
            dict2xml_element(element, v)

    elif isinstance(value, bool):
        element.text = str(value)

    elif isinstance(value, int):
        element.text = str(value)

    elif isinstance(value, str):
        element.text = value


def get_document(request):
    if request.headers['Content-Type'] != 'application/json':
        abort_by_bad_content_type(request.headers['Content-Type'])

    root = dict2xml(request.get_json())

    id = root.findtext('id', default='N/A')
    return registry_lib.Document(
        document_id=bbclib.get_new_id(id, include_timestamp=False),
        root=root
    )


def run_client():
    client = bbc_app.BBcAppClient(port=bbc_config.DEFAULT_CORE_PORT,
            multiq=False, loglevel='all')
    client.set_user_id(bbclib.get_new_id('examples.certify_web',
            include_timestamp=False))
    client.set_domain_id(domain_id)
    client.set_callback(bbc_app.Callback())
    ret = client.register_to_core()
    assert ret
    return client


api = Blueprint('api', __name__)


@api.after_request
def after_request(response):
    g.store.close()

    if g.idPubkeyMap is not None:
        g.idPubkeyMap.close()
    if g.registry is not None:
        g.registry.close()
    if g.client is not None:
        g.client.unregister_from_core()

    return response


@api.before_request
def before_request():
    g.store = Store()
    g.idPubkeyMap = None
    g.registry = None
    g.client = None


@api.route('/')
def index():
    return jsonify({})


@api.route('/digest', methods=['GET'])
def get_digest():
    document = get_document(request)
    digest = hashlib.sha256(document.file()).digest()

    return jsonify({'digest': str(binascii.b2a_hex(digest), 'utf-8')})


@api.route('/proof', methods=['GET'])
def get_proof_for_document():
    document = get_document(request)

    digest = hashlib.sha256(document.file()).digest()

    g.client = run_client()

    g.client.verify_in_ledger_subsystem(None, digest)
    dat = wait_check_result_msg_type(g.client.callback,
            bbclib.MsgType.RESPONSE_VERIFY_HASH_IN_SUBSYS)

    dic = dat[KeyType.merkle_tree]

    if dic[b'result'] == False:
        abort_by_merkle_root_not_found()

    spec = dic[b'spec']
    if spec[b'subsystem'] != b'ethereum':
        abort_by_subsystem_not_supported()

    subtree = dic[b'subtree']

    spec_s = {}
    subtree_s = []

    for k, v in spec.items():
        spec_s[k.decode()] = v.decode() if isinstance(v, bytes) else v

    for node in subtree:
        subtree_s.append({
            'position': node[b'position'].decode(),
            'digest': node[b'digest'].decode()
        })

    return jsonify({
        'proof': {
            'spec': spec_s,
            'subtree': subtree_s
        }
    })


@api.route('/register', methods=['POST'])
def register_document():
    document = get_document(request)

    registry = g.store.read_user(NAME_REGISTRY)
    user = g.store.read_user(NAME_USER)

    g.idPubkeyMap = id_lib.BBcIdPublickeyMap(domain_id)
    g.registry = registry_lib.BBcRegistry(domain_id, registry.user_id,
            registry.user_id, g.idPubkeyMap)

    g.registry.register_document(user.user_id, document,
            registry_lib.DocumentSpec(description="certificate"),
            keypair=registry.keypair)

    g.client = run_client()

    g.client.register_in_ledger_subsystem(None,
            g.registry.get_document_digest(document.document_id))
    dat = wait_check_result_msg_type(g.client.callback,
            bbclib.MsgType.RESPONSE_REGISTER_HASH_IN_SUBSYS)

    return jsonify({
        'success': 'true'
    })


@api.route('/setup', methods=['POST'])
def setup():
    g.store.setup()

    tmpclient = bbc_app.BBcAppClient(port=bbc_config.DEFAULT_CORE_PORT,
            multiq=False, loglevel="all")
    tmpclient.domain_setup(domain_id)
    tmpclient.callback.synchronize()
    tmpclient.unregister_from_core()

    g.idPubkeyMap = id_lib.BBcIdPublickeyMap(domain_id)

    user_id, keypairs = g.idPubkeyMap.create_user_id(num_pubkeys=1)
    g.store.write_user(User(user_id, NAME_REGISTRY, keypair=keypairs[0]))

    user_id, keypairs = g.idPubkeyMap.create_user_id(num_pubkeys=1)
    g.store.write_user(User(user_id, NAME_USER, keypair=keypairs[0]))

    return jsonify({'domain_id': binascii.b2a_hex(domain_id).decode()})


@api.route('/verify', methods=['GET'])
def verify_certificate():
    document = get_document(request)

    proof = request.json.get('proof')

    if proof is None:
        abort_by_missing_param('proof')

    spec = proof['spec']
    subtree = proof['subtree']

    workingdir = bbc_config.DEFAULT_WORKING_DIR

    bbcConfig = bbc_config.BBcConfig(workingdir,
            os.path.join(workingdir, bbc_config.DEFAULT_CONFIG_FILE))
    config = bbcConfig.get_config()

    prevdir = os.getcwd()
    os.chdir(bbc1.__path__[0] + '/core/ethereum')

    # private key can be anything as it is unused for viewing blockchain.
    eth = bbc_ethereum.BBcEthereum(
        spec['network'],
        0x1234123412341234123412341234123412341234123412341234123412341234,
        contract_address=spec['contract_address']
    )

    os.chdir(prevdir)

    digest = hashlib.sha256(document.file()).digest()

    block_no = eth.verify(digest, subtree)

    if block_no <= 0:
        abort_by_merkle_root_not_found()

    block = network.web3.eth.getBlock(block_no)

    return jsonify({
        'block': str(block_no),
        'time': block['timestamp']
    })


@api.errorhandler(400)
@api.errorhandler(404)
@api.errorhandler(409)
def error_handler(error):
    return jsonify({'error': {
        'code': error.description['code'],
        'message': error.description['message']
    }}), error.code


# end of api/body.py
