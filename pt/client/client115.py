import log
from pt.client.py115 import Py115
from config import Config
from pt.client.client import IDownloadClient
from utils.types import MediaType


class Client115(IDownloadClient):
    downclient = None
    lasthash = None

    def get_config(self):
        # 读取配置文件
        config = Config()
        cloudconfig = config.get_config('client115')
        if cloudconfig:
            # 解析下载目录
            self.save_path = cloudconfig.get('save_path')
            self.save_containerpath = cloudconfig.get('save_containerpath')
            self.downclient = Py115(cloudconfig.get("cookie"))

    def connect(self):
        self.downclient.login()

    def get_status(self):
        if not self.downclient:
            return False
        ret = self.downclient.login()
        if not ret:
            log.info(self.downclient.err)
            return False
        return True

    def get_torrents(self, ids=None, status=None, tag=None):
        tlist = []
        if not self.downclient:
            return tlist
        ret, tasks = self.downclient.gettasklist(page=1)
        if not ret:
            log.info("【115】获取任务列表错误：{}".format(self.downclient.err))
            return tlist
        if tasks:
            for task in tasks:
                if ids:
                    if task.get("info_hash") not in ids:
                        continue
                if status:
                    if task.get("status") not in status:
                        continue
                ret, tdir = self.downclient.getiddir(task.get("file_id"))
                task["path"] = tdir
                tlist.append(task)

        return tlist or []

    def get_completed_torrents(self, **kwargs):
        return self.get_torrents(status=[2])

    def get_downloading_torrents(self, **kwargs):
        return self.get_torrents(status=[0, 1])

    def remove_torrents_tag(self, **kwargs):
        pass

    def get_transfer_task(self, **kwargs):
        trans_tasks = []
        try:
            torrents = self.get_completed_torrents()
            for torrent in torrents:
                if torrent.get('path') == "/":
                    continue
                true_path = torrent.get('path')
                if not true_path:
                    continue
                true_path = self.get_replace_path(true_path)
                trans_tasks.append({'path': true_path, 'id': torrent.get('info_hash')})
            return trans_tasks
        except Exception as result:
            log.error("【115】异常错误：{}".format(result))
            return trans_tasks

    def get_remove_torrents(self, **kwargs):
        return []

    def add_torrent(self, content, mtype, download_dir=None, **kwargs):
        if not self.downclient:
            return False
        if download_dir:
            save_path = download_dir
        else:
            if mtype == MediaType.TV:
                save_path = self.tv_save_path
            elif mtype == MediaType.MOVIE:
                save_path = self.movie_save_path
            else:
                save_path = self.anime_save_path
        if isinstance(content, str):
            ret, self.lasthash = self.downclient.addtask(tdir=save_path, content=content)
            if not ret:
                log.error("【115】添加下载任务失败：{}".format(self.downclient.err))
                return None
            return self.lasthash
        else:
            log.info("【115】暂时不支持非链接下载")
            return None

    def delete_torrents(self, delete_file, ids):
        if not self.downclient:
            return False
        return self.downclient.deltask(thash=ids)

    def start_torrents(self, ids):
        pass

    def stop_torrents(self, ids):
        pass

    def set_torrents_status(self, ids):
        return self.delete_torrents(ids=ids, delete_file=False)

    def get_download_dirs(self):
        return []
