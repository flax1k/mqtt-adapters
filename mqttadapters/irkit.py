#!/usr/bin/env python
# -*- coding: utf-8 -*-

from zeroconf import ServiceBrowser, Zeroconf
import threading
import time
import ipaddress
import subprocess
import sys
import requests
import paho.mqtt.client as mqtt
import logging
import logging.config
import json
from argparse import ArgumentParser

SERVICE_TYPE = '_irkit._tcp.local.'
TOPIC_BASE = 'irkit/'
ERROR_TOPIC = TOPIC_BASE + 'error'
CHECK_INTERVAL_SEC = 5.0
SERVICE_TIMEOUT = 60

logger = logging.getLogger()


def get_topic(name):
    if '.' in name:
        return TOPIC_BASE + name[:name.index('.')].encode('utf8')
    else:
        return TOPIC_BASE + name.encode('utf8')


def get_messages_topic(name):
    return get_topic(name) + '/messages'


class HostListener(object):

    hosts = {}
    removed = []

    def __init__(self, mqtt_client):
        self.mqtt_client = mqtt_client
        self.finished_lock = threading.Lock()

    def remove_service(self, zeroconf, type, name):
        logger.info('Service %s removed' % (name,))
        self._refresh_hosts()
        host_info = {'status': 'removed', 'type': type, 'name': name}
        self.mqtt_client.publish(get_topic(name),
                                 payload=json.dumps(host_info))
        if name in self.hosts:
            self.hosts[name].inactivate()

    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        logger.info('Service %s added, service info: %s' % (name, info))
        self._refresh_hosts()
        if info:
            if name not in self.hosts:
                host = IRKitHost(name, info.address, info.port,
                                 self.mqtt_client)
                host.on_finished = self.on_finished
                self.hosts[name] = host
                host.start()
                logger.info('Subscribe: %s' % get_messages_topic(name))
                self.mqtt_client.subscribe(get_messages_topic(name))
            else:
                self.hosts[name].activate()

        host_info = {'status': 'added', 'type': type, 'name': name}
        self.mqtt_client.publish(get_topic(name),
                                 payload=json.dumps(host_info))

    def on_connect(self, client, userdata, flags, rc):
        logger.info('Connected rc=%d' % rc)
        client.subscribe(TOPIC_BASE + 'all/messages')
        for name in self.hosts.keys():
            client.subscribe(get_messages_topic(name))

    def on_message(self, client, userdata, msg):
        try:
            logger.info('Received: %s, %s' % (msg.topic, msg.payload))
            command = json.loads(msg.payload)
            assert(msg.topic.startswith(TOPIC_BASE))
            topic_sub = msg.topic[len(TOPIC_BASE):]
            assert(topic_sub.endswith('/messages'))
            to = topic_sub[:-len('/messages')]
            if to == 'all':
                for host in self.hosts.values():
                    host.post(command)
            else:
                for name, host in self.hosts.items():
                    if get_messages_topic(name) == msg.topic:
                        host.post(command)
        except (ValueError, IOError):
            logger.error('Unexpected error: %s' % sys.exc_info()[0])
            errorinfo = {'message': 'Error occurred: %s' % sys.exc_info()[0]}
            client.publish(ERROR_TOPIC, payload=json.dumps(errorinfo))

    def on_finished(self, name):
        logger.debug('Finished: %s' % name)
        with self.finished_lock:
            self.removed.append(name)

    def _refresh_hosts(self):
        with self.finished_lock:
            if len(self.removed) > 0:
                for name in self.removed:
                    logger.info('Removed: %s' % name)
                    del self.hosts[name]
                    self.mqtt_client.unsubscribe(get_messages_topic(name))
                self.removed = []


class ReceivedQueue(object):

    items = []

    def __init__(self, size):
        self.lock = threading.Lock()
        self.size = size

    def put(self, item):
        with self.lock:
            self.items.append(item)
            if len(self.items) > self.size:
                del self.items[-1]

    def has(self, item):
        with self.lock:
            if item in self.items:
                found = self.items.index(item)
                del self.items[found]
                return True
            else:
                return False


class IRKitHost(threading.Thread):

    on_finished = None

    def __init__(self, name, address, port, mqtt_client):
        super(IRKitHost, self).__init__()
        self.name = name
        self.host = '%s:%d' % (str(ipaddress.ip_address(address)), port)
        self.mqtt_client = mqtt_client
        self.lock = threading.RLock()
        self.sem = threading.Semaphore()
        self.service_timeout = None
        self.daemon = True
        self.queue = ReceivedQueue(5)

    def inactivate(self):
        with self.lock:
            self.service_timeout = SERVICE_TIMEOUT

    def activate(self):
        with self.lock:
            self.service_timeout = None

    def post(self, messages):
        if self.queue.has(messages):
            logger.debug('Skipped: already received messages')
        else:
            logger.info('Sending "%s"' % str(messages))
            with self.sem:
                session = requests.Session()
                resp = session.post('http://%s/messages' % self.host,
                                    data=json.dumps(messages), timeout=5.0)
                logger.info("Successful: %s" % resp.content)

    def _is_in_service(self):
        with self.lock:
            if self.service_timeout is None:
                return True
            self.service_timeout -= 1
            if self.service_timeout > 0:
                return True
            return False

    def run(self):
        while(self._is_in_service()):
            try:
                with self.sem:
                    session = requests.Session()
                    resp = session.get('http://%s/messages' % self.host,
                                       timeout=3.0)
                logger.debug('GET "%s" from %s' % (resp.content, self.host))
                if resp.content:
                    msg = resp.json()
                    topic = get_messages_topic(self.name)
                    self.queue.put(msg)
                    logger.info('Publishing... %s' % topic)
                    self.mqtt_client.publish(topic,
                                             payload=json.dumps(msg))
            except:
                logger.warning('Unexpected error: %s' % sys.exc_info()[0])
            time.sleep(CHECK_INTERVAL_SEC)
        if self.on_finished:
            self.on_finished(self.name)


def main():
    desc = '%s [Args] [Options]\nDetailed options -h or --help' % __file__
    parser = ArgumentParser(description=desc)
    parser.add_argument('-H', '--host', type=str, dest='host',
                        default='localhost', help='hostname of MQTT')
    parser.add_argument('-p', '--port', type=int, dest='port', default=1883,
                        help='port of MQTT')
    parser.add_argument('-l', '--logging', type=str, dest='logging',
                        default=None, help='path for logging.conf')

    args = parser.parse_args()

    if args.logging:
        logging.config.fileConfig(args.logging)
    else:
        logging.basicConfig()

    zeroconf = Zeroconf()
    mqtt_client = mqtt.Client()
    listener = HostListener(mqtt_client)
    mqtt_client.on_connect = listener.on_connect
    mqtt_client.on_message = listener.on_message
    mqtt_client.connect(args.host, args.port)
    browser = ServiceBrowser(zeroconf, SERVICE_TYPE, listener)
    try:
        mqtt_client.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        zeroconf.close()

if __name__ == '__main__':
    main()