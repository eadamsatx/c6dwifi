import ctypes
import socket
import threading
import time
import sys
from zeroconf import ServiceBrowser, Zeroconf
import logging

logger = logging.getLogger('c6dwifi')
logger.setLevel(logging.INFO)

libgphoto_names = ['libgphoto2.so.6', 'libgphoto2.6.dylib']

class GPhotoError(Exception):
    def __init__(self, result, message):
        self.result = result
        self.message = message
    def __str__(self):
        return self.message + ' (' + str(self.result) + ')'

class GPhoto2Binder():
    def __init__(self):
        self.gphoto = self.find_gphoto2()
        self.bind_gphoto()
        self.GP_CAPTURE_IMAGE = 0
        self.GP_CAPTURE_MOVIE = 1
        self.GP_CAPTURE_SOUND = 2

        self.GP_EVENT_UNKNOWN = 0
        self.GP_EVENT_TIMEOUT = 1
        self.GP_EVENT_FILE_ADDED = 2
        self.GP_EVENT_FOLDER_ADDED = 3
        self.GP_EVENT_CAPTURE_COMPLETE = 4

    def get_gphoto(self):
        return self.gphoto

    @staticmethod
    def find_gphoto2():
        for libgphoto_name in libgphoto_names:
            gphoto2_candidate = None
            try:
                gphoto2_candidate = ctypes.CDLL(libgphoto_name)
            except OSError:
                pass

            if gphoto2_candidate is not None:
                logger.info('Using {0}'.format(libgphoto_name))
                return gphoto2_candidate

        logger.error('No libgphoto2 found')

    def bind_gphoto(self):
        self.gphoto.gp_context_new.restype = ctypes.c_void_p
        self.gphoto.gp_camera_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.gphoto.gp_context_unref.argtypes = [ctypes.c_void_p]
        self.gphoto.gp_abilities_list_lookup_model.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.gphoto.gp_result_as_string.restype = ctypes.c_char_p
        self.gphoto.gp_log_add_func.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self.gphoto.gp_setting_set.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self.gphoto.gp_camera_set_abilities.argtypes = [ctypes.c_void_p, ctypes.Structure]

    class CameraAbilities(ctypes.Structure):
        _fields_ = [('model', (ctypes.c_char * 128)), ('data', (ctypes.c_char * 4096))]

    class CameraFilePath(ctypes.Structure):
        _fields_ = [('name', (ctypes.c_char * 128)), ('folder', (ctypes.c_char * 1024))]

    class GPhotoError(Exception):
        def __init__(self, result, message):
            self.result = result
            self.message = message

        def __str__(self):
            return self.message + ' (' + str(self.result) + ')'


class Common:
    log_label = 'Common'

    def log(self, msg, debug=False):
        logger.error('{0} {1}'.format(self.log_label, msg))

    def debug(self, msg):
        logger.error(msg)

    def start(self):
        def run():
            self.log('started thread')
            self.run()
            self.log('finished thread')

        self.log('starting thread')
        self.thread = threading.Thread(target=run)
        self.thread.start()

    def join(self, timeout=None):
        if not self.thread.isAlive():
            pass
        elif timeout:
            self.thread.join(timeout=timeout)
        else:
            self.thread.join()
        return not self.thread.isAlive()

    def shutdown(self):
        pass


class PTPIPCamera(Common):
    log_label = 'PTPIPCamera'

    def __init__(self, target, guid):
        self.context = ctypes.c_void_p()  # gphoto.gp_context_new()
        self.target = target
        self.guid = guid
        self.handle = ctypes.c_void_p()
        self.portlist = None
        self.abilitylist = None
        self.connected = False
        self.cached_root = None
        self.cached_time = 0
        self.cache_expiry = 2  # seconds
        self.gp2binder = GPhoto2Binder()

        self.gphoto = self.gp2binder.get_gphoto()

    def gphoto_check(self, result):
        if result < 0:
            message = self.gphoto.gp_result_as_string(result).decode()
            raise GPhotoError(result, message)
        return result

    def encoded_path(self):
        return ("ptpip:" + self.target).encode('utf-8')

    def encoded_guid(self):
        tmp = self.guid.split("-")
        guid = []
        l = lambda s: [s[i:i + 2:] for i in range(0, len(s), 2)][::-1]
        for i in range(0, 3):
            guid += l(tmp[i])
        guid += tmp[3]
        guid += tmp[4]
        tmp = "".join(guid).lower()
        guid = []
        for i in range(0, len(tmp), 2):
            guid.append(tmp[i:i + 2])
        guid = ":".join(guid)

        return guid.encode('utf-8')

    def connect(self):
        # allocate and initialise a new camera
        self.debug('allocate camera')
        res = self.gphoto.gp_camera_new(ctypes.pointer(self.handle))
        self.gphoto_check(res)

        # set model and guid in settings file
        self.gphoto.gp_setting_set(b"gphoto2", b"model", b"PTP/IP Camera")
        self.gphoto.gp_setting_set(b"ptp2_ip", b"guid", self.encoded_guid())

        # load abilities
        if not self.abilitylist:
            self.debug('load abilities list')
            self.abilitylist = ctypes.c_void_p()
            self.gphoto.gp_abilities_list_new(ctypes.pointer(self.abilitylist))
            res = self.gphoto.gp_abilities_list_load(self.abilitylist, self.context)
            self.gphoto_check(res)

        # search for model abilities
        self.debug('search abilities list')
        index = self.gphoto.gp_abilities_list_lookup_model(self.abilitylist, b'PTP/IP Camera')
        self.gphoto_check(index)
        self.debug('found at %d' % index)

        # load abilities
        self.debug('load abilities')
        abilities = GPhoto2Binder.CameraAbilities()
        res = self.gphoto.gp_abilities_list_get_abilities(self.abilitylist, index, ctypes.pointer(abilities))
        self.gphoto_check(res)

        # set camera abilities
        self.debug('set camera abilities')
        res = self.gphoto.gp_camera_set_abilities(self.handle, abilities)
        self.gphoto_check(res)

        # load port list
        if not self.portlist:
            self.debug('load port list')
            self.portlist = ctypes.c_void_p()
            self.gphoto.gp_port_info_list_new(ctypes.pointer(self.portlist))
            res = self.gphoto.gp_port_info_list_load(self.portlist)
            self.gphoto_check(res)

        # find port info entry
        self.debug('search for port info')
        index = self.gphoto.gp_port_info_list_lookup_path(self.portlist, self.encoded_path())
        self.gphoto_check(index)
        self.debug('found at %d' % index)

        # load port info entry
        self.debug('load port info')
        info = ctypes.c_void_p()
        res = self.gphoto.gp_port_info_list_get_info(self.portlist, index, ctypes.pointer(info))
        self.gphoto_check(res)

        # set the camera with the appropriate port info
        self.debug('set camera port')
        res = self.gphoto.gp_camera_set_port_info(self.handle, info)
        self.gphoto_check(res)

        # load the port path for debugging
        # if DEBUG:
        #     path = ctypes.c_char_p()
        #     res = self.gphoto.gp_port_info_get_path(info, ctypes.pointer(path))
        #     self.gphoto_check(res)
        #     self.debug(path.value)

        # connect to camera
        self.log('connecting...')
        res = self.gphoto.gp_camera_init(self.handle, self.context)
        self.gphoto_check(res)
        self.log('connected.')

        self.connected = True
        return True

    def disconnect(self):
        self._clear_cache()
        res = self.gphoto.gp_camera_exit(self.handle, self.context)
        self.gphoto_check(res)
        res = self.gphoto.gp_camera_unref(self.handle)
        self.gphoto_check(res)
        res = self.gphoto.gp_context_unref(self.context)
        self.gphoto_check(res)
        # FIXME: gphoto PTP/IP does not close sockets properly; try to work around?

    def _root_widget(self):
        now = time.time()
        if (not self.cached_root) or abs(now - self.cached_time) > self.cache_expiry:
            if not self.cached_root:
                self.gphoto.gp_widget_free(self.cached_root)
                self.cached_root = None
            root = ctypes.c_void_p()
            res = self.gphoto.gp_camera_get_config(self.handle, ctypes.pointer(root), self.context)
            if res >= 0:
                self.cached_root = root
                self.cached_time = now
        return self.cached_root

    def _clear_cache(self):
        if self.cached_root:
            self.gphoto.gp_widget_free(self.cached_root)
            self.cached_root = None

    def _find_widget(self, label):
        root = self._root_widget()
        if root:
            child = ctypes.c_void_p()
            res = self.gphoto.gp_widget_get_child_by_name(root, ctypes.c_char_p(label), ctypes.pointer(child))
            if res >= 0:
                return (root, child)
        return None

    widget_types = {0: 'window',
                    1: 'section',
                    2: 'text',
                    3: 'range',
                    4: 'toggle',
                    5: 'radio',
                    6: 'menu',
                    7: 'button',
                    8: 'date'}

    def _widget_type(self, pair):
        (root, child) = pair
        w_type = ctypes.c_int()
        res = self.gphoto.gp_widget_get_type(child, ctypes.pointer(w_type))
        self.gphoto_check(res)
        w_type = w_type.value
        if w_type in self.widget_types:
            return self.widget_types[w_type]
        else:
            return 'unknown'

    def _widget_value(self, pair):
        (root, child) = pair
        w_type = self._widget_type(pair)
        if w_type == 'text' or w_type == 'menu' or w_type == 'radio':
            ptr = ctypes.c_char_p()
            res = self.gphoto.gp_widget_get_value(child, ctypes.pointer(ptr))
            self.gphoto_check(res)
            return (w_type, ptr.value)
        elif w_type == 'range':
            top = ctypes.c_float()
            bottom = ctypes.c_float()
            step = ctypes.c_float()
            value = ctypes.c_float()
            res = self.gphoto.gp_widget_get_range(child, ctypes.pointer(bottom), ctypes.pointer(top), ctypes.pointer(step))
            self.gphoto_check(res)
            res = self.gphoto.gp_widget_get_value(child, ctypes.pointer(value))
            self.gphoto_check(res)
            return (w_type, value.value, bottom.value, top.value, step.value)
        elif w_type == 'toggle' or w_type == 'date':
            value = ctypes.c_int()
            res = self.gphoto.gp_widget_get_value(child, ctypes.pointer(value))
            self.gphoto_check(res)
            return (w_type, value.value)
        else:
            return None

    def _match_choice(self, pair, value):
        choices = self._widget_choices(pair)
        if isinstance(value, int):
            if (value >= 0) and (value < len(choices)):
                return choices[value]
        for (i, c) in zip(range(len(choices)), choices):
            try:
                if c == str(value):
                    return c
                elif float(c) == float(value):
                    return c
                elif int(c) == int(value):
                    return c
            except:
                pass
        if isinstance(value, str):
            return value
        else:
            return str(value)

    def _widget_set(self, pair, value):
        (root, child) = pair
        w_type = self._widget_type(pair)
        if w_type == 'toggle':
            if value:
                value = 1
            else:
                value = 0
        elif w_type == 'range':
            value = float(value)
        elif (w_type == 'radio') or (w_type == 'menu'):
            value = self._match_choice(pair, value)

        if isinstance(value, int):
            v = ctypes.c_int(value)
            res = self.gphoto.gp_widget_set_value(child, ctypes.pointer(v))
            return (res >= 0)
        elif isinstance(value, float):
            v = ctypes.c_float(float(value))
            res = self.gphoto.gp_widget_set_value(child, ctypes.pointer(v))
            return (res >= 0)
        elif isinstance(value, str):
            v = ctypes.c_char_p(value)
            res = self.gphoto.gp_widget_set_value(child, v)
            return (res >= 0)
        else:
            return False

    def _widget_choices(self, pair):
        (root, child) = pair
        w_type = self._widget_type(pair)
        if w_type == 'radio' or w_type == 'menu':
            count = self.gphoto.gp_widget_count_choices(child)
            if count > 0:
                choices = []
                for i in range(count):
                    ptr = ctypes.c_char_p()
                    res = self.gphoto.gp_widget_get_choice(child, i, ctypes.pointer(ptr))
                    self.gphoto_check(res)
                    choices.append(ptr.value)
                return choices
        return None

    def get_config(self, label):
        pair = self._find_widget(label)
        value = None
        if pair:
            value = self._widget_value(pair)
        return value

    def get_config_choices(self, label):
        pair = self._find_widget(label)
        value = None
        if pair:
            value = self._widget_choices(pair)
        return value

    def set_config(self, label, value):
        pair = self._find_widget(label)
        result = False
        if pair:
            result = self._widget_set(pair, value)
            if result:
                res = self.gphoto.gp_camera_set_config(self.handle, pair[0], self.context)
                result = (res >= 0)
        return result

    known_widgets = [
        'uilock',
        'bulb',
        'drivemode',
        'focusmode',
        'autofocusdrive',
        'manualfocusdrive',
        'eoszoom',
        'eoszoomposition',
        'eosviewfinder',
        'eosremoterelease',
        'serialnumber',
        'manufacturer',
        'cameramodel',
        'deviceversion',
        'model',
        'batterylevel',
        'lensname',
        'eosserialnumber',
        'shuttercounter',
        'availableshots',
        'reviewtime',
        'output',
        'evfmode',
        'ownername',
        'artist',
        'copyright',
        'autopoweroff',
        'imageformat',
        'imageformatsd',
        'iso',
        'whitebalance',
        'colortemperature',
        'whitebalanceadjusta',
        'whitebalanceadjustb',
        'whitebalancexa',
        'whitebalancexb',
        'colorspace'
        'exposurecompensation',
        'focusmode',
        'autoexposuremode',
        'picturestyle',
        'shutterspeed',
        'bracketmode',
        'aeb',
        'aperture',
        'capturetarget']

    def list_config(self):
        config = {}
        for k in self.known_widgets:
            config[k] = self.get_config(k)
        return config

    # XXX: this hangs waiting for response from camera
    def trigger_capture(self):
        res = self.gphoto.gp_camera_trigger_capture(self.handle, self.context)
        try:
            self.gphoto_check(res)
            return True
        except GPhotoError as e:
            self.log(str(e))
            return False

    # XXX: this hangs waiting for response from camera
    # def capture(self, capture_type=GP_CAPTURE_IMAGE):
    #     path = CameraFilePath()
    #     res = self.gphoto.gp_camera_capture(self.handle, ctypes.c_int(capture_type), ctypes.pointer(path), self.context)
    #     try:
    #         self.gphoto_check(res)
    #         return (path.folder, path.name)
    #     except GPhotoError as e:
    #         self.log(str(e))
    #         return None

    def wait_for_event(self, timeout=10):
        ev_type = ctypes.c_int()
        data = ctypes.c_void_p()
        res = self.gphoto.gp_camera_capture(self.handle,
                                       ctypes.c_int(timeout),
                                       ctypes.pointer(ev_type),
                                       ctypes.pointer(data), self.context)
        try:
            self.gphoto_check(res)
            return ev_type.value
        except GPhotoError as e:
            self.log(str(e))
            return None


class Canon6DConnection(Common):
    log_label = 'Canon6DConnection'

    def __init__(self, ip, guid, callback):
        self.ip = ip
        self.guid = guid
        self.callback = callback

    def run(self):
        self.log('started %s (%s)' % (self.ip, self.guid))
        self.camera = PTPIPCamera(self.ip, self.guid)
        try:
            self.camera.connect()
            print('connected to %s (%s)' % (self.ip, self.guid))
            self.callback(self.camera)
        except Exception as e:
            logger.error('failed for {0} ({1}) - {2}'.format(self.ip, self.guid, e))
        finally:
            try:
                self.camera.disconnect()
            except:
                pass
        self.log('shutdown %s (%s)' % (self.ip, self.guid))


class Canon6DConnector(Common):
    def __init__(self, callback):
        self.callback = callback
        self.connections = []
        zeroconf = Zeroconf()
        listener = self
        browser = ServiceBrowser(zeroconf, "_ptp._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_http._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_dlna._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_daap._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_dacp._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_touch-able._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_rsp._tcp.local.", listener)
        browser = ServiceBrowser(zeroconf, "_rsp._tcp.local.", listener)

        try:
            input("Press enter to exit...\n\n")
        finally:
            zeroconf.close()

    def connect(self, ip, guid):
        logger.error('Connecting to {0}, {1}'.format(ip, guid))
        if len(self.connections) == 0:
            connection = Canon6DConnection(ip, guid, self.callback)
            connection.start()
            self.connections.append(connection)

    def remove_service(self, zeroconf, type, name):
        print("Service %s removed" % (name,))

    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        print("Service %s added, service info: %s" % (name, info))
        if info is not None:
            try:
                guid = info.properties[b'sid.canon.com'].decode()
                ip = socket.inet_ntoa(info.address)
                self.connect(ip, guid)
            except:
                logger.error('not a canon')


def test_callback(camera):
    print('camera_main', camera.guid)
    camera.set_config('capture', 1)

    config = camera.list_config()
    print('got config')
    for k in sorted(config.keys()):
        v = config[k]
        if v and (v[0] == 'radio'):
            print(k, v, camera.get_config_choices(k))
        else:
            print(k, v)

    result = camera.set_config('aperture', '8.0')
    print('set aperture', result)
    result = camera.set_config('capturetarget', 'Memory card')
    print('set memory card', result)
    result = camera.set_config('eosremoterelease', 'Immediate')
    print('trigger capture', result)
    time.sleep(1)


def main(args):
    Canon6DConnector(test_callback)

if __name__ == "__main__":
    main(sys.argv[1:])