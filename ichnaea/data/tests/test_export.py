import json
import time

from celery.exceptions import Retry
import requests_mock

from ichnaea.async.config import EXPORT_QUEUE_PREFIX
from ichnaea.data.export import queue_length
from ichnaea.data.tasks import (
    schedule_export_reports,
    queue_reports,
)
from ichnaea.tests.base import CeleryTestCase
from ichnaea.tests.factories import (
    CellFactory,
    WifiFactory,
)
from ichnaea.util import decode_gzip


class BaseTest(object):

    def add_reports(self, number=3, api_key='test', email=None):
        reports = []
        for i in range(number):
            report = {
                'timestamp': time.time() * 1000.0,
                'position': {},
                'cellTowers': [],
                'wifiAccessPoints': [],
            }
            cell = CellFactory.build()
            report['position']['latitude'] = cell.lat
            report['position']['longitude'] = cell.lon
            report['position']['accuracy'] = 17 + i
            cell_data = {
                'radioType': cell.radio.name,
                'mobileCountryCode': cell.mcc,
                'mobileNetworkCode': cell.mnc,
                'locationAreaCode': cell.lac,
                'cellId': cell.cid,
                'primaryScramblingCode': cell.psc,
                'signalStrength': -110 + i,
            }
            report['cellTowers'].append(cell_data)
            wifis = WifiFactory.build_batch(2, lat=cell.lat, lon=cell.lon)
            for wifi in wifis:
                wifi_data = {
                    'macAddress': wifi.key,
                    'signalStrength': -90 + i,
                }
                report['wifiAccessPoints'].append(wifi_data)
            reports.append(report)

        queue_reports.delay(
            reports=reports, api_key=api_key, email=email).get()
        return reports

    def queue_length(self, redis_key):
        return queue_length(self.redis_client, redis_key)


class TestExporter(BaseTest, CeleryTestCase):

    def setUp(self):
        super(TestExporter, self).setUp()
        self.celery_app.export_queues = {
            'test': {
                'url': None,
                'source_apikey': 'export_source',
                'batch': 3,
                'redis_key': EXPORT_QUEUE_PREFIX + 'test',
            },
            'everything': {
                'url': None,
                'batch': 5,
                'redis_key': EXPORT_QUEUE_PREFIX + 'everything',
            },
            'no_test': {
                'url': None,
                'source_apikey': 'test',
                'batch': 2,
                'redis_key': EXPORT_QUEUE_PREFIX + 'no_test',
            },
        }
        self.prefix = EXPORT_QUEUE_PREFIX

    def test_enqueue_reports(self):
        self.add_reports(4)
        self.add_reports(1, api_key='test2')
        expected = [
            (EXPORT_QUEUE_PREFIX + 'test', 5),
            (EXPORT_QUEUE_PREFIX + 'everything', 5),
            (EXPORT_QUEUE_PREFIX + 'no_test', 1),
        ]
        for key, num in expected:
            self.assertEqual(self.queue_length(key), num)

    def test_one_queue(self):
        self.add_reports(3)
        triggered = schedule_export_reports.delay().get()
        self.assertEqual(triggered, 1)
        # data from one queue was processed
        expected = [
            (EXPORT_QUEUE_PREFIX + 'test', 0),
            (EXPORT_QUEUE_PREFIX + 'everything', 3),
            (EXPORT_QUEUE_PREFIX + 'no_test', 0),
        ]
        for key, num in expected:
            self.assertEqual(self.queue_length(key), num)

    def test_one_batch(self):
        self.add_reports(5)
        schedule_export_reports.delay().get()
        self.assertEqual(self.queue_length(EXPORT_QUEUE_PREFIX + 'test'), 2)

    def test_multiple_batches(self):
        self.add_reports(10)
        schedule_export_reports.delay().get()
        self.assertEqual(self.queue_length(EXPORT_QUEUE_PREFIX + 'test'), 1)


class TestUploader(BaseTest, CeleryTestCase):

    def setUp(self):
        super(TestUploader, self).setUp()
        self.celery_app.export_queues = {
            'test': {
                'url': 'http://127.0.0.1:9/v2/geosubmit?key=external',
                'batch': 3,
                'redis_key': EXPORT_QUEUE_PREFIX + 'test',
            },
        }
        self.prefix = EXPORT_QUEUE_PREFIX

    def test_upload(self):
        reports = self.add_reports(3, email='secretemail@localhost')

        with requests_mock.Mocker() as mock:
            mock.register_uri('POST', requests_mock.ANY, text='{}')
            schedule_export_reports.delay().get()

        self.assertEqual(mock.call_count, 1)
        req = mock.request_history[0]

        # check headers
        self.assertEqual(req.headers['Content-Type'], 'application/json')
        self.assertEqual(req.headers['Content-Encoding'], 'gzip')
        self.assertEqual(req.headers['User-Agent'], 'ichnaea')

        # check body
        body = decode_gzip(req.body)
        # make sure we don't accidentally leak emails
        self.assertFalse('secretemail' in body)

        # make sure a standards based json can decode this data
        # and none of our internal_json structures end up in it
        send_reports = json.loads(body)['items']
        self.assertEqual(len(send_reports), 3)
        expect = [report['position']['accuracy'] for report in reports]
        gotten = [report['position']['accuracy'] for report in send_reports]
        self.assertEqual(set(expect), set(gotten))

        self.check_stats(
            counter=[('items.export.test.batches', 1, 1),
                     ('items.export.test.upload_status.200', 1)],
            timer=['items.export.test.upload'],
        )

    def test_upload_retried(self):
        self.add_reports(3)

        with requests_mock.Mocker() as mock:
            mock.register_uri('POST', requests_mock.ANY, [
                {'text': '', 'status_code': 500},
                {'text': '{}', 'status_code': 404},
                {'text': '{}', 'status_code': 200},
            ])
            # simulate celery retry handling
            for i in range(5):
                try:
                    schedule_export_reports.delay().get()
                except Retry:
                    continue
                else:
                    break
                self.fail('Task should have succeeded')

        self.assertEqual(mock.call_count, 3)
        self.check_stats(
            counter=[('items.export.test.batches', 1, 1),
                     ('items.export.test.upload_status.200', 1),
                     ('items.export.test.upload_status.404', 1),
                     ('items.export.test.upload_status.500', 1)],
            timer=[('items.export.test.upload', 3)],
        )
