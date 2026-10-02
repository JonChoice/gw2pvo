import base64
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, Optional

import requests

__author__ = "Mark Ruys"
__copyright__ = "Copyright 2017, Mark Ruys"
__license__ = "MIT"
__email__ = "mark@paracas.nl"


class GoodWeApi:
    WEB_LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
    WEB_FLOW_PATH = "/sems-plant/api/stations/flow"
    WEB_SEED_TOKEN = '{"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}'
    SUCCESS_CODE = "00000"
    RETRY_LOGIN_CODE = "C0602"
    REQUEST_TIMEOUT = 10

    def __init__(self, system_id, account, password):
        self.system_id = system_id
        self.account = account
        self.password = password

        self.web_session = requests.Session()
        self.web_api: Optional[str] = None
        self.web_uuid: Optional[str] = None
        self.web_auth_data: Optional[Dict[str, Any]] = None

    def statusText(self, status):
        try:
            status = int(status)
        except (TypeError, ValueError):
            return 'Unknown'
        labels = {-1: 'Offline', 0: 'Waiting', 1: 'Normal', 2: 'Fault'}
        return labels[status] if status in labels else 'Unknown'

    def _default_result(self):
        return {
            'status': 'Unknown',
            'pgrid_w': 0,
            'eday_kwh': 0,
            'etotal_kwh': 0,
            'grid_voltage': 0,
            'pv_voltage': 0,
            'load': 0,
            'batteryPercentage': 0,
            'batteryPower': 0,
            'gridPower': 0,
            'latitude': None,
            'longitude': None
        }

    def _to_float(self, value, default=0.0):
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _kw_to_w(self, value):
        return round(self._to_float(value) * 1000)

    def _encode_web_password(self, password):
        md5hex = hashlib.md5(password.encode('utf-8')).hexdigest()
        return base64.b64encode(md5hex.encode('utf-8')).decode('utf-8')

    def _new_signature(self):
        return uuid.uuid4().hex

    def _base_web_headers(self, req_uuid: str, token_value: str):
        return {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'origin': 'https://semsplus.goodwe.com',
            'referer': 'https://semsplus.goodwe.com/',
            'currentlang': 'en',
            'neutral': '0',
            'user-agent': 'Mozilla/5.0',
            'uuid': req_uuid,
            'token': token_value,
            'x-signature': self._new_signature(),
        }

    def _build_login_headers(self, seed_uuid: str):
        return self._base_web_headers(seed_uuid, self.WEB_SEED_TOKEN)

    def _build_flow_headers(self):
        token_json = json.dumps(self.web_auth_data, separators=(',', ':'), ensure_ascii=False)
        return self._base_web_headers(str(self.web_uuid), token_json)

    def _web_login(self):
        self.web_session = requests.Session()

        seed_uuid = uuid.uuid4().hex
        payload = {
            'account': self.account,
            'pwd': self._encode_web_password(self.password),
            'agreement': 1,
            'isLocal': False,
            'isChinese': False
        }

        logging.debug("GoodWe web login request url=%s payload=%s", self.WEB_LOGIN_URL, {'account': self.account, 'pwd': '***'})
        r = self.web_session.post(
            self.WEB_LOGIN_URL,
            headers=self._build_login_headers(seed_uuid),
            json=payload,
            timeout=self.REQUEST_TIMEOUT
        )
        logging.debug("GoodWe web login response status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()

        body = r.json()
        data = body.get('data')
        if str(body.get('code')) != self.SUCCESS_CODE or not isinstance(data, dict):
            raise Exception("GoodWe web login failed: code={} description={}".format(body.get('code'), body.get('description')))

        self.web_api = (data.get('api') or '').rstrip('/')
        self.web_uuid = str(data.get('uuid') or seed_uuid)

        auth_data = dict(data)
        auth_data['uuid'] = self.web_uuid
        self.web_auth_data = auth_data

        if not self.web_api:
            raise Exception("GoodWe web login failed: missing api")

    def _fetch_flow_data(self):
        if not self.web_api or not self.web_uuid or not self.web_auth_data:
            self._web_login()

        url = self.web_api + self.WEB_FLOW_PATH
        params = {'stationId': self.system_id}

        logging.debug("GoodWe web flow request url=%s params=%s", url, params)
        r = self.web_session.get(
            url,
            headers=self._build_flow_headers(),
            params=params,
            timeout=self.REQUEST_TIMEOUT
        )
        logging.debug("GoodWe web flow response status=%s body=%s", r.status_code, r.text)
        r.raise_for_status()

        body = r.json()
        code = str(body.get('code'))

        if code == self.SUCCESS_CODE and isinstance(body.get('data'), dict):
            return body['data']

        if code == self.RETRY_LOGIN_CODE:
            raise PermissionError("GoodWe web flow C0602 account login abnormal")

        logging.warning("GoodWe web flow returned non-success code: %s", body.get('code'))
        return None

    def _map_flow_result(self, data):
        result = self._default_result()
        result.update({
            'status': self.statusText(data.get('status')),
            'pgrid_w': self._kw_to_w(data.get('pSystem', 0)),
            'load': self._kw_to_w(data.get('pConsum', data.get('pAc', 0))),
            'batteryPercentage': round(self._to_float(data.get('soc'), 0)),
            'batteryPower': self._kw_to_w(data.get('pBat', 0)),
            'gridPower': self._kw_to_w(data.get('pGrid', 0)),
        })

        message = "{status}, {pgrid_w} W now, {eday_kwh} kWh today, {etotal_kwh} kWh all time, {grid_voltage} V grid, {pv_voltage} V PV, Battery {batteryPower} W output, {gridPower} Grid Power, {batteryPercentage} battery".format(**result)
        if result['status'] in {'Normal', 'Offline'}:
            logging.info(message)
        else:
            logging.warning(message)

        return result

    def getCurrentReadings(self):
        ''' Download the most recent readings from the GoodWe SEMS Plus web API. '''
        for attempt in range(2):
            try:
                data = self._fetch_flow_data()
                if isinstance(data, dict):
                    return self._map_flow_result(data)
                break
            except PermissionError as exp:
                logging.warning("%s", exp)
                if attempt == 0:
                    self._web_login()
                    continue
            except requests.exceptions.RequestException as exp:
                logging.warning("GoodWe web flow request failed: %s", exp)
                break
            except Exception as exp:
                logging.warning("GoodWe web flow parse failed: %s", exp)
                break

        return self._default_result()
