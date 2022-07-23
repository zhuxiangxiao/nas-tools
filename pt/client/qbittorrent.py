import os
import qbittorrentapi
import log
from config import Config, PT_TAG
from pt.client.client import IDownloadClient
from utils.types import MediaType


class Qbittorrent(IDownloadClient):

    __force_upload = False
    qbc = None

    def get_config(self):
        """
        获取配置
        """
        # 读取配置文件
        config = Config()
        qbittorrent = config.get_config('qbittorrent')
        if qbittorrent:
            self.host = qbittorrent.get('qbhost')
            self.port = int(qbittorrent.get('qbport'))
            self.username = qbittorrent.get('qbusername')
            self.password = qbittorrent.get('qbpassword')
            # 强制做种开关
            self.__force_upload = qbittorrent.get('force_upload')
            # 解析下载目录
            self.save_path = qbittorrent.get('save_path')
            self.save_containerpath = qbittorrent.get('save_containerpath')

    def connect(self):
        """
        连接
        """
        if self.host and self.port:
            self.qbc = self.__login_qbittorrent()

    def __login_qbittorrent(self):
        """
        连接qbittorrent
        :return: qbittorrent对象
        """
        try:
            # 登录
            qbt = qbittorrentapi.Client(host=self.host,
                                        port=self.port,
                                        username=self.username,
                                        password=self.password,
                                        VERIFY_WEBUI_CERTIFICATE=False)
            return qbt
        except Exception as err:
            log.error("【QB】qBittorrent连接出错：%s" % str(err))
            return None

    def get_status(self):
        """
        检查连通性
        :return: True、Fals
        """
        try:
            data, _ = self.get_pt_data()
            if data is None:
                return False
            return True
        except Exception as e:
            print(str(e))
            return False

    def get_torrents(self, ids=None, status=None, tag=None):
        """
        按条件读取种子信息
        :param ids: 种子ID，单个ID或者ID列表
        :param status: 种子状态过滤
        :param tag: 种子标签过滤
        :return: 种子信息列表
        """
        if not self.qbc:
            return []
        self.qbc.auth_log_in()
        torrents = self.qbc.torrents_info(torrent_hashes=ids, status_filter=status, tag=tag)
        self.qbc.auth_log_out()
        return torrents or []

    def get_completed_torrents(self, tag=None):
        """
        读取完成的种子信息
        """
        if not self.qbc:
            return []
        return self.get_torrents(status=["completed"], tag=tag)

    def get_downloading_torrents(self, tag=None):
        """
        读取下载中的种子信息
        """
        if not self.qbc:
            return []
        return self.get_torrents(status=["downloading"], tag=tag)

    def remove_torrents_tag(self, ids, tag):
        """
        移除种子Tag
        :param ids: 种子Hash列表
        :param tag: 标签内容
        """
        return self.qbc.torrents_delete_tags(torrent_hashes=ids, tags=tag)

    def set_torrents_status(self, ids):
        """
        迁移完成后设置种子标签为 已整理
        :param ids: 种子ID列表
        """
        if not self.qbc:
            return
        self.qbc.auth_log_in()
        # 删除标签
        self.qbc.torrents_remove_tags(tags=PT_TAG, torrent_hashes=ids)
        # 打标签
        self.qbc.torrents_add_tags(tags="已整理", torrent_hashes=ids)
        # 超级做种
        if self.__force_upload:
            self.qbc.torrents_set_force_start(enable=True, torrent_hashes=ids)
        log.info("【QB】设置qBittorrent种子状态成功")
        self.qbc.auth_log_out()

    def torrents_set_force_start(self, ids):
        """
        设置强制作种
        """
        self.qbc.auth_log_in()
        self.qbc.torrents_set_force_start(enable=True, torrent_hashes=ids)
        self.qbc.auth_log_out()

    def get_transfer_task(self, tag):
        """
        获取需要转移的种子列表
        :return: 替换好路径的种子文件路径清单
        """
        # 处理下载完成的任务
        torrents = self.get_completed_torrents(tag=tag)
        trans_tasks = []
        for torrent in torrents:
            # 判断标签是否包含"已整理"
            if torrent.get("tags") and "已整理" in torrent.get("tags"):
                continue
            true_path = torrent.get('content_path', os.path.join(torrent.get('save_path'), torrent.get('name')))
            if not true_path:
                continue
            true_path = self.get_replace_path(true_path)
            trans_tasks.append({'path': true_path, 'id': torrent.get('hash')})
        return trans_tasks

    def get_remove_torrents(self, seeding_time, tag):
        """
        获取需要清理的种子清单
        :param seeding_time: 保种时间，单位秒
        :param tag: 种子标签
        :return: 种子ID列表
        """
        if not seeding_time:
            return []
        torrents = self.get_completed_torrents(tag=tag)
        remove_torrents = []
        for torrent in torrents:
            if not torrent.get('seeding_time'):
                continue
            if int(torrent.get('seeding_time')) > int(seeding_time):
                log.info("【PT】%s 做种时间：%s（秒），已达清理条件，进行清理..." % (torrent.get('name'), torrent.get('seeding_time')))
                remove_torrents.append(torrent.get('hash'))
        return remove_torrents
    def get_completed_not_seed_torrents(self,seeding_trackers):
        """
        查询可以清单的种子
        :param seeding_trackers: 做种白名单关键词
        :return: 可以清理的种子ID列表
        """
        return []

    def get_last_add_torrentid_by_tag(self, tag, status=None):
        """
        根据种子的下载链接获取下载中或暂停的钟子的ID
        :return: 种子ID
        """
        if not status:
            status = ["paused"]
        torrents = self.get_torrents(status=status, tag=tag)
        if torrents:
            return torrents[0].get("hash")
        else:
            return None

    def add_torrent(self, content, mtype, is_paused=None, tag=None):
        """
        添加qbittorrent下载任务
        :param content: 种子数据
        :param mtype: 媒体类型：电影、电视剧、动漫
        :param is_paused: 是否默认暂停，只有需要进行下一步控制时，才会添加种子时默认暂停
        :param tag: 下载时对种子的标记
        """
        if not self.qbc or not content:
            return False
        self.qbc.auth_log_in()
        if mtype == MediaType.TV:
            save_path = self.tv_save_path
            category = self.tv_category
        elif mtype == MediaType.MOVIE:
            save_path = self.movie_save_path
            category = self.movie_category
        else:
            save_path = self.anime_save_path
            category = self.anime_category
        if isinstance(content, str):
            qbc_ret = self.qbc.torrents_add(urls=content,
                                            save_path=save_path,
                                            category=category,
                                            is_paused=is_paused,
                                            tags=tag)
        else:
            qbc_ret = self.qbc.torrents_add(torrent_files=content,
                                            save_path=save_path,
                                            category=category,
                                            is_paused=is_paused,
                                            tags=tag)
        self.qbc.auth_log_out()
        return True if qbc_ret and str(qbc_ret).find("Ok") != -1 else False

    def start_torrents(self, ids):
        """
        下载控制：开始
        """
        if not self.qbc:
            return False
        return self.qbc.torrents_resume(torrent_hashes=ids)

    def stop_torrents(self, ids):
        """
        下载控制：停止
        """
        if not self.qbc:
            return False
        return self.qbc.torrents_pause(torrent_hashes=ids)

    def delete_torrents(self, delete_file, ids):
        """
        删除种子
        """
        if not self.qbc:
            return False
        if not ids:
            return False
        self.qbc.auth_log_in()
        ret = self.qbc.torrents_delete(delete_files=delete_file, torrent_hashes=ids)
        self.qbc.auth_log_out()
        return ret

    def get_files(self, tid):
        """
        获取种子文件列表
        """
        return self.qbc.torrents_files(torrent_hash=tid)

    def set_files(self, torrent_hash, file_ids, priority):
        """
        设置下载文件的状态，priority为0为不下载，priority为1为下载
        """
        if not torrent_hash or not file_ids:
            return False
        self.qbc.torrents_file_priority(torrent_hash=torrent_hash, file_ids=file_ids, priority=priority)
        return True

    def get_pt_data(self):
        """
        获取PT下载软件中当前上传和下载量
        :return: 上传量、下载量
        """
        if not self.qbc:
            return 0, 0
        transfer_info = self.qbc.transfer_info()
        if transfer_info:
            return transfer_info.get("up_info_data"), transfer_info.get("dl_info_data")
        return None, None
