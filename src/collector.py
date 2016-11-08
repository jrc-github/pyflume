#! -*- coding:utf-8 -*-

import sys
import signal
import logging
import traceback

from kafka import KafkaProducer
from kafka.errors import KafkaError


class CollectorProxy(object):

    def __init__(self, config):
        self.log = logging.getLogger(config.get('LOG', 'LOG_HANDLER'))
        self.channel = None
        self.exit_flag = False
        self.collectors = dict()
        for section in config.sections():
            if section.startswith('COLLECTOR:'):
                collector_type = config.get(section, 'TYPE')
                if collector_type.lower() == 'kafka':
                    collector_name = section[section.find(':') + 1:]
                    self.collectors[collector_name] = KafkaCollector(config, section)
                else:
                    pass  # 错误处理

    def process_data(self):
        chn = self.channel()
        while not self.exit_flag:
            _msg = chn.get()
            if isinstance(_msg, str) and 'stop' == _msg:
                continue
            _target_collector = self.collectors.get(_msg['collector'])
            self.log.info(_msg)
            _target_collector.process_data(_msg)

    def run(self, channel=None):
        def _exit(*args, **kwargs):
            self.log.info('Received sigterm, collector is going down.')
            self.exit_flag = True
            # 解除阻塞
            self.channel().put('stop')

        signal.signal(signal.SIGTERM, _exit)

        self.log.info('Pyflume collector starts.')
        self.channel = channel
        self.process_data()
        self.log.info('Pyflume collector ends.')


class Collector(object):

    def __init__(self, config=None):
        self.log = logging.getLogger(config.get('LOG', 'LOG_HANDLER'))

    def process_data(self, msg):
        _data = msg['filename'] + ': ' + msg['data']
        sys.stdout.write(_data)
        sys.stdout.flush()


class KafkaCollector(Collector):

    def __init__(self, config, section):
        super(KafkaCollector, self).__init__(config)
        self.log.debug(config.get(section, 'SERVER'))
        self.config = config
        self.section = section
        self.topic = config.get(section, 'TOPIC')
        self.producer = None

    def process_data(self, msg):
        _data = msg['filename'] + ': ' + msg['data']
        self.log.debug(msg['collector'] + _data)

        self.producer = KafkaProducer(bootstrap_servers=self.config.get(self.section, 'SERVER'))

        future = self.producer.send(self.topic, _data)
        # Block for 'synchronous' sends
        try:
            record_metadata = future.get(timeout=10)
        except KafkaError:
            # Decide what to do if produce request failed...
            self.log.error(traceback.format_exc())
            return None
        finally:
            self.producer.close()

        return record_metadata.topic, record_metadata.partition, record_metadata.offset
