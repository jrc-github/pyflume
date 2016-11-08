#! -*- coding:utf-8 -*-

import logging
import pickle
import platform
import select
import signal
import threading
import traceback
import os
import re
import json
from time import sleep
from errno import EINTR

from wrappers import pickle_lock

if platform.system() == 'Linux':
    from inotify import Inotify
    from inotify import IN_CREATE, IN_MOVED_FROM, IN_MOVED_TO, IN_MODIFY, IN_DELETE
else:
    from select import KQ_FILTER_SIGNAL, KQ_FILTER_READ, KQ_EV_ADD


class AgentBase(object):

    def __init__(self, config):
        self.logger = logging.getLogger(config.get('LOG', 'LOG_HANDLER'))
        self.pickle_File = config.get('TEMP', 'PICKLE_FILE')
        self.pool_path = config.get('POOL', 'POOL_PATH')
        self.filename_pattern = re.compile(config.get('POOL', 'FILENAME_PATTERN'))
        self.collector_name = config.get('POOL', 'COLLECTOR')
        self.pickle_handler = None
        self.pickle_data = None
        self.handlers = list()
        self.pid = None
        self.exit_flag = False
        self.channel = None

    @pickle_lock
    def _load_pickle(self):
        if not os.path.exists(self.pickle_File):
            try:
                self.pickle_handler = open(self.pickle_File, 'w+')
            except IOError:
                self.logger.error(traceback.format_exc())
                exit(-1)

        self.pickle_handler = open(self.pickle_File, 'r+')
        try:
            self.pickle_data = pickle.load(self.pickle_handler)
            self.logger.debug('load pickle_data:' + str(self.pickle_data))
        except EOFError:
            self.logger.warning('No pickle data exits.')
            self.pickle_data = dict()

    @pickle_lock
    def _update_pickle(self, file_handler):
        self.logger.debug('before update pickle_data:' + str(self.pickle_data))
        # 数据已成功采集,更新offset
        self.pickle_data[file_handler.name] = file_handler.tell()
        self.logger.debug('after update pickle_data:' + str(self.pickle_data))
        # 从文件读取pickle_data时offset已经改变,现在重置到0
        self.pickle_handler.seek(0)
        pickle.dump(self.pickle_data, self.pickle_handler)

    @pickle_lock
    def _reset_pickle(self, file_names):
        self.logger.debug('before reset pickle_data:' + str(self.pickle_data))
        for name in file_names:
            self.pickle_data[name] = 0
            self.pickle_handler.seek(0)
            pickle.dump(self.pickle_data, self.pickle_handler)
        self.logger.debug('after reset pickle_data:' + str(self.pickle_data))

    @pickle_lock
    def _get_pickle_data(self, file_name):

        return self.pickle_data[file_name]

    def _get_log_data_by_handler(self, handler):
        # 先获取文件上次读取的偏移量
        _file_name = handler.name
        try:
            _offset = self._get_pickle_data(_file_name)
        except KeyError:
            self.logger.warning('Cant find "%s" in pickles.' % _file_name)
            _offset = 0
        handler.seek(_offset)
        _data = handler.readlines()
        if _data:
            return _data
        else:
            return None

    def get_handlers(self):
        handlers = list()
        _files = os.listdir(self.pool_path)
        for _file in _files:
            if self.is_need_monitor(_file):
                if os.path.isfile(os.path.join(self.pool_path, _file)):
                    try:
                        _handler = open(os.path.join(self.pool_path, _file), 'r')
                        handlers.append(_handler)
                    except IOError:
                        self.logger.error(traceback.format_exc())
                        return []

        return handlers

    def is_need_monitor(self, filename):
        if self.filename_pattern.match(filename):
            return True
        return False

    def msg_join(self, filename, data):
        msg = dict()
        msg['collector'] = self.collector_name
        msg['filename'] = filename
        msg['data'] = data
        return msg


class KqueueAgent(AgentBase):

    def __init__(self, config):
        super(KqueueAgent, self).__init__(config)

    def monitor_file_kqueue(self):
        _thread_move = threading.Thread(target=self.monitor_file_move, name='monitor_file_move')
        _thread_content = threading.Thread(target=self.monitor_file_content, name='monitor_file_content')

        _thread_move.start()
        _thread_content.start()

        signal.pause()  # 阻塞这里，等待信号

        _thread_move.join()
        _thread_content.join()

    def monitor_file_move(self):
        _is_first_time = True
        before_directories = set()

        while not self.exit_flag:
            try:
                after_directories = set(os.listdir(self.pool_path))
                xor_set = after_directories ^ before_directories
                if _is_first_time:
                    # 加入此判断是为防止pyflume启动时，pickle不为空导致数据重复导入
                    before_directories = after_directories
                    _is_first_time = False
                    continue
                if xor_set:
                    xor_set_list = [os.path.join(self.pool_path, name) for name in xor_set]
                    self._reset_pickle(xor_set_list)
                    os.kill(self.pid, signal.SIGUSR1)
                    before_directories = after_directories
                    self.logger.debug(str(xor_set) + 'These files are added or deleted;')
            except:
                self.logger.error(traceback.format_exc())
                os.kill(self.pid, signal.SIGKILL)
                exit(-1)

            sleep(10)

    def monitor_file_content(self):

        while not self.exit_flag:
            try:
                break_flag = True
                self.handlers = self.get_handlers()
                self._load_pickle()
                kq = select.kqueue()
                _monitor_list = list()
                _monitor_dict = dict()
                for _handler in self.handlers:
                    _monitor_list.append(
                        select.kevent(_handler.fileno(), filter=KQ_FILTER_READ, flags=KQ_EV_ADD)
                    )
                    _monitor_dict[_handler.fileno()] = _handler
                _monitor_list.append(
                    select.kevent(signal.SIGUSR1, filter=KQ_FILTER_SIGNAL, flags=KQ_EV_ADD)
                )
                # 此时另一个线程负责监控文件的变动,例如新文件的移入,另一个线程会发送信号终止这个循环,重新遍历获取所有文件的句柄.
                while break_flag:
                    revents = kq.control(_monitor_list, 3)
                    for event in revents:
                        if event.filter == select.KQ_FILTER_READ:
                            _in_process_file_handler = _monitor_dict[event.ident]
                            _data = self._get_log_data_by_handler(_in_process_file_handler)
                            if not _data:
                                continue
                            else:
                                chn = self.channel()
                                for _line in _data:
                                    chn.put(self.msg_join(filename=_in_process_file_handler.name, data=_line))
                            # 数据完整性由collector来保证
                            self._update_pickle(_in_process_file_handler)
                        elif event.filter == select.KQ_FILTER_SIGNAL:
                            if event.ident == signal.SIGUSR1:
                                self.logger.info(u'捕捉到信号SIGUSR1')
                                # 关闭文件句柄，避免过量问题
                                for _handler in self.handlers:
                                    _handler.close()
                                break_flag = False
            except Exception:
                self.logger.error(traceback.format_exc())
                os.kill(self.pid, signal.SIGKILL)
                exit(-1)
            finally:
                self.pickle_handler.close()


class InotifyAgent(AgentBase):

    def __init__(self, config):
        super(InotifyAgent, self).__init__(config)
        self.__monitor_dict = dict()

    def monitor_file_inotify(self):
        try:
            self.handlers = self.get_handlers()
            self._load_pickle()
            _inotify = Inotify()
            _inotify.add_watch(self.pool_path, IN_CREATE | IN_MOVED_FROM | IN_MOVED_TO |
                               IN_MODIFY | IN_DELETE)
            _epoll = select.epoll()
            _epoll.register(_inotify.fileno(), select.EPOLLIN)

            for _in_process_file_handler in self.handlers:
                self.__monitor_dict[_in_process_file_handler.name] = _in_process_file_handler
                file_size = os.path.getsize(_in_process_file_handler.name)
                while _in_process_file_handler.tell() < file_size:
                    _data = self._get_log_data_by_handler(_in_process_file_handler)
                    if not _data:
                        break
                    chn = self.channel()
                    for _line in _data:
                        chn.put(self.msg_join(filename=_in_process_file_handler.name, data=_line))
                        # 数据完整性由collector来保证
                    self._update_pickle(_in_process_file_handler)

            while not self.exit_flag:
                try:
                    for fd, epoll_event in _epoll.poll(-1):
                        if fd == _inotify.fileno():
                            for inotify_event in _inotify.read_events():
                                self.process_event(inotify_event)
                                if self.exit_flag:
                                    break
                except IOError as e:
                    if e.errno == EINTR:
                        continue
                    raise e
        except Exception:
            self.logger.error(traceback.format_exc())
            exit(-1)
        finally:
            self.pickle_handler.close()

    def process_event(self, inotify_event):
        (watch_path, mask, cookie, filename) = inotify_event
        if mask & IN_CREATE or mask & IN_MOVED_TO:
            if self.is_need_monitor(filename):
                full_filename = os.path.join(watch_path, filename)
                if os.path.isfile(full_filename):
                    _new_file_handler = open(full_filename, 'r')
                    self.__monitor_dict[full_filename] = _new_file_handler
                    self.handlers.append(_new_file_handler)
                    self._update_pickle(_new_file_handler)
        elif mask & IN_MODIFY:
            _in_process_file_handler = self.__monitor_dict.get(os.path.join(watch_path, filename))
            if _in_process_file_handler:
                _data = self._get_log_data_by_handler(_in_process_file_handler)
                if _data:
                    chn = self.channel()
                    for _line in _data:
                        chn.put(self.msg_join(filename=_in_process_file_handler.name, data=_line))
                        # 数据完整性由collector来保证
                    self._update_pickle(_in_process_file_handler)
        elif mask & IN_DELETE or mask & IN_MOVED_FROM:
            _delete_file_handler = self.__monitor_dict.get(os.path.join(watch_path, filename))
            if _delete_file_handler:
                self.__monitor_dict.pop(os.path.join(watch_path, filename))
                _delete_file_handler.close()
                self.handlers.remove(_delete_file_handler)
                self._reset_pickle([_delete_file_handler.name])


class Agent(InotifyAgent, KqueueAgent):

    def __init__(self, config):
        super(Agent, self).__init__(config)

    def run(self, channel=None):
        self.pid = os.getpid()

        def _kqueue_exit(*args, **kwargs):
            self.logger.info('Received sigterm, agent is going down.')
            self.exit_flag = True
            os.kill(self.pid, signal.SIGUSR1)

        def _inotify_exit(*args, **kwargs):
            self.logger.info('Received sigterm, agent is going down.')
            self.exit_flag = True

        self.logger.info('Pyflume agent starts.')
        self.channel = channel
        if platform.system() == 'Linux':
            signal.signal(signal.SIGTERM, _inotify_exit)
            self.monitor_file_inotify()
        else:
            signal.signal(signal.SIGTERM, _kqueue_exit)
            self.monitor_file_kqueue()
        self.logger.info('Pyflume agent ends.')
