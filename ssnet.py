import struct, socket, errno, select
from helpers import *

HDR_LEN = 8


CMD_EXIT = 0x4200
CMD_PING = 0x4201
CMD_PONG = 0x4202
CMD_CONNECT = 0x4203
CMD_CLOSE = 0x4204
CMD_EOF = 0x4205
CMD_DATA = 0x4206

cmd_to_name = {
    CMD_EXIT: 'EXIT',
    CMD_PING: 'PING',
    CMD_PONG: 'PONG',
    CMD_CONNECT: 'CONNECT',
    CMD_CLOSE: 'CLOSE',
    CMD_EOF: 'EOF',
    CMD_DATA: 'DATA',
}
    


def _nb_clean(func, *args):
    try:
        return func(*args)
    except OSError, e:
        if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise
        else:
            return None


def _try_peername(sock):
    try:
        return sock.getpeername()
    except socket.error, e:
        if e.args[0] not in (errno.ENOTCONN, errno.ENOTSOCK):
            raise
        else:
            return ('0.0.0.0',0)


class SockWrapper:
    def __init__(self, rsock, wsock):
        self.rsock = rsock
        self.wsock = wsock
        self.peername = _try_peername(self.rsock)
        self.shut_read = self.shut_write = False
        self.buf = []

    def __del__(self):
        log('%r: deleting\n' % self)

    def __repr__(self):
        return 'SW%r' % (self.peername,)

    def noread(self):
        if not self.shut_read:
            log('%r: done reading\n' % self)
            self.shut_read = True
            #self.rsock.shutdown(socket.SHUT_RD)  # doesn't do anything anyway
        
    def nowrite(self):
        if not self.shut_write:
            log('%r: done writing\n' % self)
            self.shut_write = True
            try:
                self.wsock.shutdown(socket.SHUT_WR)
            except socket.error:
                pass

    def uwrite(self, buf):
        self.wsock.setblocking(False)
        try:
            return _nb_clean(os.write, self.wsock.fileno(), buf)
        except OSError:
            # unexpected error... stream is dead
            self.nowrite()
            self.noread()
            return 0
        
    def write(self, buf):
        assert(buf)
        return self.uwrite(buf)

    def uread(self):
        if self.shut_read:
            return
        self.rsock.setblocking(False)
        try:
            return _nb_clean(os.read, self.rsock.fileno(), 65536)
        except OSError:
            return '' # unexpected error... we'll call it EOF

    def fill(self):
        if self.buf:
            return
        rb = self.uread()
        if rb:
            self.buf.append(rb)
        if rb == '':  # empty string means EOF; None means temporarily empty
            self.noread()

    def copy_to(self, outwrap):
        if self.buf and self.buf[0]:
            wrote = outwrap.write(self.buf[0])
            self.buf[0] = self.buf[0][wrote:]
        while self.buf and not self.buf[0]:
            self.buf[0:1] = []
        if not self.buf and self.shut_read:
            outwrap.nowrite()


class Handler:
    def __init__(self, socks = None, callback = None):
        self.ok = True
        self.socks = set(socks or [])
        if callback:
            self.callback = callback

    def pre_select(self, r, w, x):
        r |= self.socks

    def callback(self):
        log('--no callback defined-- %r\n' % self)
        (r,w,x) = select.select(self.socks, [], [], 0)
        for s in r:
            v = s.recv(4096)
            if not v:
                log('--closed-- %r\n' % self)
                self.socks = set()
                self.ok = False


class Proxy(Handler):
    def __init__(self, wrap1, wrap2):
        Handler.__init__(self, [wrap1.rsock, wrap1.wsock,
                                wrap2.rsock, wrap2.wsock])
        self.wrap1 = wrap1
        self.wrap2 = wrap2

    def pre_select(self, r, w, x):
        if self.wrap1.buf:
            w.add(self.wrap2.wsock)
        elif not self.wrap1.shut_read:
            r.add(self.wrap1.rsock)
        if self.wrap2.buf:
            w.add(self.wrap1.wsock)
        elif not self.wrap2.shut_read:
            r.add(self.wrap2.rsock)

    def callback(self):
        self.wrap1.fill()
        self.wrap2.fill()
        self.wrap1.copy_to(self.wrap2)
        self.wrap2.copy_to(self.wrap1)
        if (self.wrap1.shut_read and self.wrap2.shut_read and
            not self.wrap1.buf and not self.wrap2.buf):
            self.ok = False


class Mux(Handler):
    def __init__(self, rsock, wsock):
        Handler.__init__(self, [rsock, wsock])
        self.rsock = rsock
        self.wsock = wsock
        self.new_channel = None
        self.channels = {}
        self.chani = 0
        self.want = 0
        self.inbuf = ''
        self.outbuf = []
        self.send(0, CMD_PING, 'chicken')

    def next_channel(self):
        # channel 0 is special, so we never allocate it
        for timeout in xrange(1024):
            self.chani += 1
            if self.chani > 65535:
                self.chani = 1
            if not self.channels.get(self.chani):
                return self.chani
            
    def send(self, channel, cmd, data):
        data = str(data)
        assert(len(data) <= 65535)
        p = struct.pack('!ccHHH', 'S', 'S', channel, cmd, len(data)) + data
        self.outbuf.append(p)
        log(' > channel=%d cmd=%s len=%d\n' 
            % (channel, cmd_to_name[cmd], len(data)))
        #log('Mux: send queue is %d/%d\n' 
        #    % (len(self.outbuf), sum(len(b) for b in self.outbuf)))

    def got_packet(self, channel, cmd, data):
        log('<  channel=%d cmd=%s len=%d\n' 
            % (channel, cmd_to_name[cmd], len(data)))
        if cmd == CMD_PING:
            self.send(0, CMD_PONG, data)
        elif cmd == CMD_PONG:
            log('received PING response\n')
        elif cmd == CMD_EXIT:
            self.ok = False
        elif cmd == CMD_CONNECT:
            assert(not self.channels.get(channel))
            if self.new_channel:
                self.new_channel(channel, data)
        else:
            callback = self.channels[channel]
            callback(cmd, data)

    def flush(self):
        self.wsock.setblocking(False)
        if self.outbuf and self.outbuf[0]:
            wrote = _nb_clean(os.write, self.wsock.fileno(), self.outbuf[0])
            if wrote:
                self.outbuf[0] = self.outbuf[0][wrote:]
        while self.outbuf and not self.outbuf[0]:
            self.outbuf[0:1] = []

    def fill(self):
        self.rsock.setblocking(False)
        b = _nb_clean(os.read, self.rsock.fileno(), 32768)
        #log('<<< %r\n' % b)
        if b == '': # EOF
            self.ok = False
        if b:
            self.inbuf += b

    def handle(self):
        self.fill()
        #log('inbuf is: (%d,%d) %r\n'
        #     % (self.want, len(self.inbuf), self.inbuf))
        while 1:
            if len(self.inbuf) >= (self.want or HDR_LEN):
                (s1,s2,channel,cmd,datalen) = \
                    struct.unpack('!ccHHH', self.inbuf[:HDR_LEN])
                assert(s1 == 'S')
                assert(s2 == 'S')
                self.want = datalen + HDR_LEN
            if self.want and len(self.inbuf) >= self.want:
                data = self.inbuf[HDR_LEN:self.want]
                self.inbuf = self.inbuf[self.want:]
                self.want = 0
                self.got_packet(channel, cmd, data)
            else:
                break

    def pre_select(self, r, w, x):
        r.add(self.rsock)
        if self.outbuf:
            w.add(self.wsock)

    def callback(self):
        (r,w,x) = select.select([self.rsock], [self.wsock], [], 0)
        if self.rsock in r:
            self.handle()
        if self.outbuf and self.wsock in w:
            self.flush()


class MuxWrapper(SockWrapper):
    def __init__(self, mux, channel):
        SockWrapper.__init__(self, mux.rsock, mux.wsock)
        self.mux = mux
        self.channel = channel
        self.mux.channels[channel] = self.got_packet
        log('new channel: %d\n' % channel)

    def __del__(self):
        self.nowrite()
        SockWrapper.__del__(self)

    def __repr__(self):
        return 'SW%r:Mux#%d' % (self.peername,self.channel)

    def noread(self):
        if not self.shut_read:
            self.shut_read = True

    def nowrite(self):
        if not self.shut_write:
            self.shut_write = True
            self.mux.send(self.channel, CMD_EOF, '')

    def uwrite(self, buf):
        if len(buf) > 65535:
            buf = buf[:32768]
        self.mux.send(self.channel, CMD_DATA, buf)
        return len(buf)

    def uread(self):
        if self.shut_read:
            return '' # EOF
        else:
            return None  # no data available right now

    def got_packet(self, cmd, data):
        if cmd == CMD_CLOSE:
            self.noread()
            self.nowrite()
        elif cmd == CMD_EOF:
            self.noread()
        elif cmd == CMD_DATA:
            self.buf.append(data)
        else:
            raise Exception('unknown command %d (%d bytes)' 
                            % (cmd, len(data)))


def connect_dst(ip, port):
    outsock = socket.socket()
    outsock.setsockopt(socket.SOL_IP, socket.IP_TTL, 42)
    try:
        outsock.connect((ip,port))
    except socket.error, e:
        if e.args[0] not in [errno.ECONNREFUSED]:
            raise
    return SockWrapper(outsock,outsock)