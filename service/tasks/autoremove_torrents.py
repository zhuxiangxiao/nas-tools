from threading import Lock

import log
from pt.downloader import Downloader

lock = Lock()


class AutoRemoveTorrents:
    downloader = None

    def __init__(self):
        self.downloader = Downloader()

    def run_schedule(self):
        """
        运行自动删除定时服务
        """
        try:
            lock.acquire()
            if self.downloader:
                self.downloader.pt_removetorrents()
        except Exception as err:
            log.error("【RUN】执行任务autoremovetorrents出错：%s" % str(err))
        finally:
            lock.release()
        try:
            lock.acquire()
            if self.downloader:
                self.downloader.pt_remove_not_seed_torrents()
        except Exception as err:
            log.error("【RUN】执行任务pt_remove_not_seed_torrents出错：%s" % str(err))
        finally:
            lock.release()