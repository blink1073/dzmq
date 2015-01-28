# Copyright (C) 2013 Open Source Robotics Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import socket
import zmq
import uuid
import os
import logging
import platform
import struct
import threading
import signal
import sys
import netifaces

from bson import BSON, Binary
import numpy as np

from .utils import get_log


# Defaults and overrides
ADV_SUB_PORT = 11312
DZMQ_PORT_KEY = 'DZMQ_BCAST_PORT'
DZMQ_HOST_KEY = 'DZMQ_BCAST_HOST'
DZMQ_IP_KEY = 'DZMQ_IP'
DZMQ_IFACE_KEY = 'DZMQ_IFACE'

# Constants
OP_ADV = 0x01
OP_SUB = 0x02
UDP_MAX_SIZE = 512
GUID_LENGTH = 16
ADV_REPEAT_PERIOD = 1.0
VERSION = 0x0001
TOPIC_MAXLENGTH = 192
FLAGS_LENGTH = 16
ADDRESS_MAXLENGTH = 267
DEBUG = True


def from_bson(data):
    def unpack(obj):
        for (key, value) in obj.items():
            if isinstance(value, dict):
                if 'shape' in value and 'dtype' in value and 'data' in value:
                    obj[key] = np.frombuffer(value['data'],
                                             dtype=value['dtype'])
                    obj[key] = obj[key].reshape(value['shape'])
                else:
                    # Make sure to recurse into sub-dicts
                    obj[key] = unpack(value)
        return obj
    return unpack(BSON(data).decode())


def to_bson(obj):
    for (key, value) in obj.items():
        if isinstance(value, np.ndarray):
            obj[key] = dict(shape=value.shape,
                            dtype=value.dtype.str,
                            data=Binary(value.tobytes()))
        elif isinstance(value, dict):  # Make sure we recurse into sub-dicts
            obj[key] = to_bson(value)
    return BSON.encode(obj)


class DZMQ(object):

    """
    This class provides a basic pub/sub system with discovery.  Basic
    publisher:

    import pybsonmq
    d = pybsonmq.DZMQ()
    d.advertise('foo')
    msg = 'bar'
    while True:
        d.publish('foo', msg)
        d.spinOnce(0.1)

    Basic subscriber:

    from __future__ import print_function
    import pybsonmq
    d = pybsonmq.DZMQ()
    d.subscribe('foo', lambda topic,msg: print('Got %s on %s'%(topic,msg)))
    d.spin()

    """

    def __init__(self, context=None, log=None):
        self.context = context or zmq.Context.instance()
        self.log = log or get_log()
        self.guid = uuid.uuid4()

        if DEBUG:
            self.log.setLevel(logging.DEBUG)

        # Determine network addresses.  Look at environment variables, and
        # fall back on defaults.

        # What IP address will we give to others to use when contacting us?
        self.ipaddr = None
        if DZMQ_IP_KEY in os.environ:
            self.ipaddr = os.environ[DZMQ_IP_KEY]
        elif DZMQ_IFACE_KEY in os.environ:
            addrs = get_local_addresses(ifaces=[os.environ[DZMQ_IFACE_KEY]])
            if addrs:
                self.ipaddr = addrs[0]
        if not self.ipaddr:
            # Try to find a non-loopback interface and then compute a broadcast
            # address from it.
            addrs = get_local_addresses()
            self.log.info(addrs)
            non_local_addrs = [x for x in addrs if not x.startswith('127')]
            if len(non_local_addrs) == 0:
                # Oh, well.
                self.ipaddr = addrs[0]
            else:
                # Take the first non-local one.
                self.ipaddr = non_local_addrs[0]

        # What's our broadcast port?
        if DZMQ_PORT_KEY in os.environ:
            self.bcast_port = int(os.environ[DZMQ_PORT_KEY])
        else:
            # Take the default
            self.bcast_port = ADV_SUB_PORT
        # What's our broadcast host?
        if DZMQ_HOST_KEY in os.environ:
            self.bcast_host = os.environ[DZMQ_HOST_KEY]
        else:
            # TODO: consider computing a more specific broadcast address based
            # on the result of get_local_addresses()
            # The following line isn't correct because it doesn't take account
            # of the netmask, but it allows the code to run without sudo
            # on OSX.
            self.bcast_host = '.'.join(self.ipaddr.split('.')[:-1] + ['255'])

        # Set up to listen to broadcasts
        self.bcast_recv = socket.socket(socket.AF_INET,  # Internet
                                        socket.SOCK_DGRAM)  # UDP
        self.bcast_recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if platform.system() in ['Darwin']:
            self.bcast_recv.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_REUSEPORT, 1)
        try:
            self.bcast_recv.bind((self.bcast_host, self.bcast_port))
            self.log.info("Opened (%s, %s)" %
                          (self.bcast_host, self.bcast_port))
        except Exception:
            self.log.error("Could not open (%s, %s)" %
                           (self.bcast_host, self.bcast_port))
            raise
        # Set up to send broadcasts
        self.bcast_send = socket.socket(socket.AF_INET,  # Internet
                                        socket.SOCK_DGRAM)  # UDP
        self.bcast_send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Bookkeeping (which should be cleaned up)
        self.publishers = []
        self.subscribers = []
        self.sub_connections = []
        self.poller = zmq.Poller()
        # TODO: figure out what happens with multiple classes
        self.adv_timer = None
        self._advertisement_repeater()
        signal.signal(signal.SIGINT, self._sighandler)

        # Set up the one pub and one sub socket that we'll use
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket_addrs = []
        tcp_addr = 'tcp://%s' % (self.ipaddr)
        tcp_port = self.pub_socket.bind_to_random_port(tcp_addr)
        tcp_addr += ':%d' % (tcp_port)
        if len(tcp_addr) > ADDRESS_MAXLENGTH:
            raise Exception('TCP address length %d exceeds maximum %d'
                            % (len(tcp_addr), ADDRESS_MAXLENGTH))
        self.pub_socket_addrs.append(tcp_addr)
        self.pub_socket.setsockopt(zmq.LINGER, 0)
        self.sub_socket = self.context.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.LINGER, 0)
        self.sub_socket_addrs = []

        self.poller.register(self.bcast_recv, zmq.POLLIN)
        self.poller.register(self.pub_socket, zmq.POLLIN)
        self.poller.register(self.sub_socket, zmq.POLLIN)

    def _sighandler(self, sig, frame):
        self.adv_timer.cancel()
        sys.exit(0)

    def _advertise(self, publisher):
        """
        Internal method to pack and broadcast ADV message.
        """
        msg = ''
        msg += struct.pack('<H', VERSION)
        # TODO: pack the GUID more efficiently; should only need one call to
        # struct.pack()
        for i in range(0, GUID_LENGTH):
            msg += struct.pack('<B', (self.guid.int >> i * 8) & 0xFF)
        msg += struct.pack('<B', len(publisher['topic']))
        msg += publisher['topic']
        msg += struct.pack('<B', OP_ADV)
        # Flags unused for now
        flags = [0x00] * FLAGS_LENGTH
        msg += struct.pack('<%dB' % (FLAGS_LENGTH), *flags)
        # We'll announce once for each address
        for addr in publisher['addresses']:
            if addr.startswith('inproc'):
                # Don't broadcast inproc addresses
                continue
            # Struct objects copy by value
            mymsg = msg
            mymsg += struct.pack('<H', len(addr))
            mymsg += addr
            self.bcast_send.sendto(mymsg, (self.bcast_host, self.bcast_port))

    def advertise(self, topic):
        """
        Advertise the given topic.  Do this before calling publish().
        """
        if len(topic) > TOPIC_MAXLENGTH:
            raise Exception('Topic length %d exceeds maximum %d'
                            % (len(topic), TOPIC_MAXLENGTH))
        publisher = {}
        publisher['socket'] = self.pub_socket
        inproc_addr = 'inproc://%s' % topic
        # TODO: consider race condition in this test and set:
        if inproc_addr not in self.pub_socket_addrs:
            publisher['socket'].bind(inproc_addr)
            self.pub_socket_addrs.append(inproc_addr)
        tcp_addr = [a for a in self.pub_socket_addrs if a.startswith('tcp')][0]
        publisher['addresses'] = [inproc_addr, tcp_addr]
        publisher['topic'] = topic
        self.publishers.append(publisher)
        self._advertise(publisher)

        # Also connect to internal subscribers, if there are any
        adv = {}
        adv['topic'] = topic
        adv['address'] = inproc_addr
        adv['guid'] = self.guid
        for sub in self.subscribers:
            if sub['topic'] == topic:
                self._connect_subscriber(adv)

    def unadvertise(self, topic):
        raise Exception("unadvertise not implemented")

    def _subscribe(self, subscriber):
        """
        Internal method to pack and broadcast SUB message.
        """
        msg = ''
        msg += struct.pack('<H', VERSION)
        # TODO: pack the GUID more efficiently; should only need one call to
        # struct.pack()
        for i in range(0, GUID_LENGTH):
            msg += struct.pack('<B', (self.guid.int >> i * 8) & 0xFF)
        msg += struct.pack('<B', len(subscriber['topic']))
        msg += subscriber['topic']
        msg += struct.pack('<B', OP_SUB)
        # Flags unused for now
        flags = [0x00] * FLAGS_LENGTH
        msg += struct.pack('<%dB' % FLAGS_LENGTH, *flags)
        # Null body
        self.bcast_send.sendto(msg, (self.bcast_host, self.bcast_port))

    def subscribe(self, topic, cb):
        """
        Subscribe to the given topic.  Received messages will be passed to
        given the callback, which should have the signature: cb(topic, msg).
        """
        # Record what we're doing
        subscriber = {}
        subscriber['topic'] = topic
        subscriber['cb'] = cb
        self.subscribers.append(subscriber)
        self._subscribe(subscriber)

        # Also connect to internal publishers, if there are any
        adv = {}
        adv['topic'] = subscriber['topic']
        adv['address'] = 'inproc://%s' % subscriber['topic']
        adv['guid'] = self.guid
        for pub in self.publishers:
            if pub['topic'] == subscriber['topic']:
                self._connect_subscriber(adv)

    def unsubscribe(self, topic):
        raise Exception("unsubscribe not implemented")

    def publish(self, topic, msg):
        """
        Publish the given message on the given topic.  You should have called
        advertise() on the topic first.
        """
        if [p for p in self.publishers if p['topic'] == topic]:
            msg = to_bson(msg)
            with open('log.txt', 'a') as fid:
                fid.write('Sending\n')
            self.pub_socket.send_multipart((topic, msg))

    def _handle_adv_sub(self, msg):
        """
        Internal method to handle receipt of SUB and ADV messages.
        """
        try:
            data, addr = msg
            # Unpack the header
            offset = 0
            version = struct.unpack_from('<H', data, offset)[0]
            if version != VERSION:
                self.log.warn('Warning: mismatched protocol versions: %d != %d'
                              % (version, VERSION))
            offset += 2
            guid_int = 0
            for i in range(0, GUID_LENGTH):
                guid_int += struct.unpack_from('<B', data, offset)[0] << 8 * i
                offset += 1
            guid = uuid.UUID(int=guid_int)
            topiclength = struct.unpack_from('<B', data, offset)[0]
            offset += 1
            topic = data[offset:offset + topiclength]
            offset += topiclength
            op = struct.unpack_from('<B', data, offset)[0]
            offset += 1
            flags = struct.unpack_from('<%dB' % FLAGS_LENGTH, data, offset)
            offset += FLAGS_LENGTH

            if op == OP_ADV:
                # Unpack the ADV body
                adv = {}
                adv['topic'] = topic
                adv['guid'] = guid
                adv['flags'] = flags
                addresslength = struct.unpack_from('<H', data, offset)[0]
                offset += 2
                adv['address'] = data[offset:offset + addresslength]
                offset += addresslength

                # Are we interested in this topic?
                if [s for s in self.subscribers if s['topic'] == adv['topic']]:
                    # Yes, we're interested; make a connection
                    self._connect_subscriber(adv)

            elif op == OP_SUB:
                # The SUB body is NULL
                # If we're publishing this topic, re-advertise it to allow the
                # new subscriber to find us.
                [self._advertise(p) for p in self.publishers
                 if p['topic'] == topic]

            else:
                self.log.warn('Warning: got unrecognized OP: %d' % op)

        except Exception as e:
            self.log.warn('Warning: exception while processing SUB or ADV '
                          'message: %s' % e)

    def _connect_subscriber(self, adv):
        """
        Internal method to connect to a publisher.
        """
        # Choose the best address to use.  If the publisher's GUID is the same
        # as our GUID, then we must both be in the same process, in which case
        # we'd like to use an 'inproc://' address.  Otherwise, fall back on
        # 'tcp://'.
        if adv['address'].startswith('tcp'):
            if adv['guid'] == self.guid:
                # Us; skip it
                return
        elif adv['address'].startswith('inproc'):
            if adv['guid'] != self.guid:
                # Not us; skip it
                return
        else:
            self.log.warn('Warning: ingoring unknown address type: %s' %
                          (adv['address']))
            return

        # Are we already connected to this publisher for this topic?
        if [c for c in self.sub_connections
                if c['topic'] == adv['topic'] and c['guid'] == adv['guid']]:
            return
        # Connect our subscriber socket
        conn = {}
        conn['socket'] = self.sub_socket
        conn['topic'] = adv['topic']
        conn['address'] = adv['address']
        conn['guid'] = adv['guid']
        conn['socket'].setsockopt(zmq.SUBSCRIBE, adv['topic'])
        self.sub_connections.append(conn)
        conn['socket'].connect(adv['address'])
        self.log.info('Connected to %s for %s (%s != %s)' %
                      (adv['address'], adv['topic'], adv['guid'], self.guid))

    def _advertisement_repeater(self):
        [self._advertise(p) for p in self.publishers]
        self.adv_timer = threading.Timer(
            ADV_REPEAT_PERIOD, self._advertisement_repeater)
        self.adv_timer.start()

    def spinOnce(self, timeout=-1):
        """
        Check once for incoming messages, invoking callbacks for received
        messages.  Wait for up to timeout seconds.  For no waiting, set
        timeout=0. To wait forever, set timeout=-1.
        """
        if timeout < 0:
            # zmq interprets timeout=None as infinite
            timeout = None
        else:
            # zmq wants the timeout in milliseconds
            timeout = int(timeout * 1e3)
        # Look for sockets that are ready to read
        events = self.poller.poll(timeout)
        # Process the events
        for e in events:
            # Is it the broadcast socket, which we manage?
            # Todo: handle heartbeat/status checks
            if e[0] == self.bcast_recv.fileno():
                # Assume that we get the whole message in one go
                self._handle_adv_sub(self.bcast_recv.recvfrom(UDP_MAX_SIZE))
            else:
                # Must be a zmq socket
                sock = e[0]
                # Get the message (assuming that we get it all in one read)
                topic, msg = sock.recv_multipart()
                self.log.debug('Got message: %s' % topic)
                # Invoke all the callbacks registered for this topic.
                [s['cb'](topic, from_bson(msg)) for s in self.subscribers
                 if s['topic'] == topic]

    def spin(self):
        """
        Give control to the message event loop.
        """
        while True:
            self.spinOnce(0.01)

# Stolen from rosgraph
# https://github.com/ros/ros_comm/blob/hydro-devel/tools/rosgraph/src/rosgraph/network.py
# cache for performance reasons
_local_addrs = None


def get_local_addresses(use_ipv6=False, ifaces=None):
    """
    :returns: known local addresses. Not affected by ROS_IP/ROS_HOSTNAME,
``[str]``
    """
    # cache address data as it can be slow to calculate
    global _local_addrs
    if _local_addrs is not None:
        return _local_addrs

    local_addrs = None
    ifaces = ifaces or netifaces.interfaces()

    v4addrs = []
    v6addrs = []
    for iface in ifaces:
        try:
            ifaddrs = netifaces.ifaddresses(iface)
        except ValueError:
            # even if interfaces() returns an interface name
            # ifaddresses() might raise a ValueError
            # https://bugs.launchpad.net/ubuntu/+source/netifaces/+bug/753009
            continue
        if socket.AF_INET in ifaddrs:
            v4addrs.extend([addr['addr']
                            for addr in ifaddrs[socket.AF_INET]])
        if socket.AF_INET6 in ifaddrs:
            v6addrs.extend([addr['addr']
                            for addr in ifaddrs[socket.AF_INET6]])
    if use_ipv6:
        local_addrs = v6addrs + v4addrs
    else:
        local_addrs = v4addrs
    _local_addrs = local_addrs
    return local_addrs
