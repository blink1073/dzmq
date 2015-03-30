from .. import DZMQ
try:
    from bson import BSON
except ImportError:
    BSON = None
    import json

import time
import logging
try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO
try:
    import numpy as np
except ImportError:
    np = None


class TestPubSub(object):

    def setup(self):
        self.pub = DZMQ()

        self.sub = DZMQ()
        self.sub.log.setLevel(logging.DEBUG)

        sobj = StringIO()
        hdlr = logging.StreamHandler(sobj)
        hdlr.setLevel(logging.DEBUG)
        self.sub.log.addHandler(hdlr)
        self.io_hdlr = hdlr

    def get_log(self):
        output = self.io_hdlr.stream.getvalue().strip()
        self.io_hdlr.stream.truncate(0)
        return output

    def test_basic(self):
        self.pub.advertise('what_what')
        time.sleep(0.1)
        payload = {'foo': 'bar'}

        def cb(msg):
            assert msg == payload

        self.sub.subscribe('what_what', cb)
        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('what_what', payload)
        time.sleep(0.1)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Connected to" in output
        assert "Got message: what_what" in output

    def test_multiple_topics(self):
        self.pub.advertise('hey_hey')
        self.pub.advertise('boo_boo')
        payload = {'spam': 100}

        time.sleep(0.1)

        def cb(msg):
            assert msg == payload

        self.sub.subscribe('hey_hey', cb)
        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('hey_hey', payload)
        time.sleep(0.1)
        self.pub.publish('boo_boo', payload)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Connected to" in output
        assert "Got message: hey_hey" in output

    def test_multiple_cbs(self):
        self.pub.advertise('yeah_yeah')
        if np:
            payload = {'eggs': np.random.random(100)}
        else:
            payload = {'eggs': [1, 2, 3]}

        time.sleep(0.1)

        def cb1(msg):
            self.sub.log.debug('Got cb1')
            assert 'eggs' in msg

        def cb2(msg):
            self.sub.log.debug('Got cb2')
            assert 'eggs' in msg

        self.sub.subscribe('yeah_yeah', cb1)
        self.sub.subscribe('yeah_yeah', cb2)
        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('yeah_yeah', payload)
        time.sleep(0.1)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Connected to" in output
        assert "Got message: yeah_yeah" in output
        assert 'Got cb1' in output
        assert 'Got cb2' in output

    def test_unadvertise(self):
        self.pub.advertise('yeah_yeah')
        payload = {'spam': 100}

        time.sleep(0.1)

        def cb(msg):
            assert False

        self.pub.unadvertise('yeah_yeah')
        self.sub.subscribe('yeah_yeah', cb)

        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('yeah_yeah', payload)
        time.sleep(0.1)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Connected to" in output
        assert "Got message: yeah_yeah" not in output

    def test_unsubscribe(self):
        self.pub.advertise('yeah_yeah')
        payload = {'spam': 100}

        time.sleep(0.1)

        def cb(msg):
            assert msg == payload

        self.sub.subscribe('yeah_yeah', cb)

        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('yeah_yeah', payload)
        time.sleep(0.1)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Connected to" in output
        assert "Got message: yeah_yeah" in output

        self.sub.unsubscribe('yeah_yeah')

        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('yeah_yeah', payload)
        time.sleep(0.1)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Got message: yeah_yeah" not in output, output

    def test_raw_sub(self):
        self.pub.advertise('what_what')
        time.sleep(0.1)
        payload = {'foo': 'bar', 'bar': {'baz': [1, 2, 3]}}

        def cb(msg):
            if BSON:
                assert msg == BSON.encode(payload)
            else:
                assert msg == json.loads(payload)

        self.sub.subscribe('what_what', cb, raw=True)
        self.sub.spinOnce()

        time.sleep(0.1)
        self.pub.publish('what_what', payload)
        time.sleep(0.1)
        self.sub.spinOnce()

        # check the output
        output = self.get_log()
        assert "Connected to" in output
        assert "Got message: what_what" in output

    def teardown(self):
        self.pub.close()
        self.sub.close()