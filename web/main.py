import _thread
import logging
import os.path
import shutil
import signal
import subprocess
import importlib
from math import floor
from subprocess import call
import requests
from flask import Flask, request, json, render_template, make_response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

import log
from message.channel.wechat import WeChat
from pt.sites import Sites
from rmt.doubanv2api.douban import Douban
from service.sync import Sync
from service.run import stop_monitor, restart_monitor
from pt.client.qbittorrent import Qbittorrent
from pt.client.transmission import Transmission
from pt.downloader import Downloader
from pt.rss import Rss
from pt.searcher import Searcher
from rmt.filetransfer import FileTransfer
from rmt.media import Media
from pt.media_server import MediaServer
from rmt.metainfo import MetaInfo
from pt.mediaserver.jellyfin import Jellyfin
from pt.mediaserver.plex import Plex
from service.tasks.autoremove_torrents import AutoRemoveTorrents
from service.tasks.douban_sync import DoubanSync
from service.tasks.pt_signin import PTSignin
from service.tasks.pt_transfer import PTTransfer
from service.tasks.rss_download import RSSDownloader
from message.send import Message
from config import WECHAT_MENU, PT_TRANSFER_INTERVAL, LOG_QUEUE
from service.run import stop_scheduler, restart_scheduler
from service.scheduler import Scheduler
from utils.functions import get_used_of_partition, str_filesize, str_timelong, get_system, get_dir_files_by_ext
from utils.sqls import get_search_result_by_id, get_search_results, \
    get_transfer_history, get_transfer_unknown_paths, \
    update_transfer_unknown_state, delete_transfer_unknown, get_transfer_path_by_id, insert_transfer_blacklist, \
    delete_transfer_log_by_id, get_config_site, insert_config_site, get_site_by_id, delete_config_site, \
    update_config_site, get_config_search_rule, update_config_search_rule, get_config_rss_rule, update_config_rss_rule, \
    get_unknown_path_by_id, get_rss_tvs, get_rss_movies, delete_rss_movie, delete_rss_tv, \
    get_users, insert_user, delete_user, get_transfer_statistics, get_system_messages
from utils.types import MediaType, SearchType, DownloaderType, SyncType, OsType
from version import APP_VERSION
from web.backend.douban_hot import DoubanHot
from web.backend.subscribe import add_rss_subscribe, add_rss_substribe_from_string
from web.backend.webhook_event import WebhookEvent
from web.backend.search_torrents import search_medias_for_web
from utils.WXBizMsgCrypt3 import WXBizMsgCrypt
import xml.etree.cElementTree as ETree

from message.channel.telegram import Telegram

login_manager = LoginManager()
login_manager.login_view = "login"


def create_flask_app(config):
    """
    创建Flask实例，定时前端WEB的所有请求接口及页面访问
    """
    app_cfg = config.get_config('app') or {}
    admin_user = app_cfg.get('login_user') or "admin"
    admin_password = app_cfg.get('login_password') or "password"
    ADMIN_USERS = [{
        "id": 0,
        "name": admin_user,
        "password": admin_password[6:],
        "pris": "我的媒体库,资源搜索,推荐,订阅管理,下载管理,媒体识别,服务,系统设置,搜索设置"
    }]

    App = Flask(__name__)
    App.config['JSON_AS_ASCII'] = False
    App.secret_key = 'jxxghp'
    applog = logging.getLogger('werkzeug')
    applog.setLevel(logging.ERROR)
    login_manager.init_app(App)

    def stop_service():
        """
        停止所有服务
        """
        # 停止定时服务
        stop_scheduler()
        # 停止监控
        stop_monitor()
        # 签退
        logout_user()

    def shutdown_server():
        """
        停卡Flask进程
        """
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        os.kill(os.getpid(), sig)

    def check_process(pname):
        """
        判断进程是否存在
        """
        if not pname:
            return False
        text = subprocess.Popen('ps -ef | grep -v grep | grep %s' % pname, shell=True).communicate()
        return True if text else False

    @App.after_request
    def add_header(r):
        """
        统一添加Http头，标用缓存，避免Flask多线程+Chrome内核会发生的静态资源加载出错的问题
        """
        r.headers["Cache-Control"] = "no-cache, no-store, max-age=0"
        r.headers["Pragma"] = "no-cache"
        r.headers["Expires"] = "0"
        return r

    def get_user(user_name):
        """
        根据用户名获得用户记录
        """
        for user in ADMIN_USERS:
            if user.get("name") == user_name:
                return user
        for user in get_users():
            if user[1] == user_name:
                return {"id": user[0], "name": user[1], "password": user[2], "pris": user[3]}
        return {}

    class User(UserMixin):
        """
        用户
        """

        def __init__(self, user):
            self.username = user.get('name')
            self.password_hash = user.get('password')
            self.id = user.get('id')

        def verify_password(self, password):
            """
            验证密码
            """
            if self.password_hash is None:
                return False
            return check_password_hash(self.password_hash, password)

        def get_id(self):
            """
            获取用户ID
            """
            return self.id

        @staticmethod
        def get(user_id):
            """
            根据用户ID获取用户实体，为 login_user 方法提供支持
            """
            if user_id is None:
                return None
            for user in ADMIN_USERS:
                if user.get('id') == user_id:
                    return User(user)
            for user in get_users():
                if not user:
                    continue
                if user[0] == user_id:
                    return User({"id": user[0], "name": user[1], "password": user[2], "pris": user[3]})
            return None

    # 定义获取登录用户的方法
    @login_manager.user_loader
    def load_user(user_id):
        return User.get(user_id)

    # 页面不存在
    @App.errorhandler(404)
    def page_not_found(error):
        return render_template("404.html", error=error), 404

    # 服务错误
    @App.errorhandler(500)
    def page_server_error(error):
        return render_template("500.html", error=error), 500

    # 主页面
    @App.route('/', methods=['GET', 'POST'])
    def login():
        # 判断当前的运营环境
        SystemFlag = 0
        if get_system() == OsType.LINUX and check_process("supervisord"):
            SystemFlag = 1
        if request.method == 'GET':
            GoPage = request.args.get("next") or ""
            if GoPage.startswith('/'):
                GoPage = GoPage[1:]
            if current_user.is_authenticated:
                userid = current_user.id
                username = current_user.username
                pris = get_user(username).get("pris")
                if userid is None or username is None:
                    return render_template('login.html',
                                           GoPage=GoPage)
                else:
                    return render_template('navigation.html',
                                           GoPage=GoPage,
                                           UserName=username,
                                           UserPris=str(pris).split(","),
                                           SystemFlag=SystemFlag,
                                           AppVersion=APP_VERSION)
            else:
                return render_template('login.html',
                                       GoPage=GoPage)

        else:
            GoPage = request.form.get('next') or ""
            if GoPage.startswith('/'):
                GoPage = GoPage[1:]
            username = request.form.get('username')
            if not username:
                return render_template('login.html',
                                       GoPage=GoPage,
                                       err_msg="请输入用户名")
            password = request.form.get('password')
            user_info = get_user(username)
            if not user_info:
                return render_template('login.html',
                                       GoPage=GoPage,
                                       err_msg="用户名或密码错误")
            # 创建用户实体
            user = User(user_info)
            # 校验密码
            if user.verify_password(password):
                # 创建用户 Session
                login_user(user)
                pris = user_info.get("pris")
                return render_template('navigation.html',
                                       GoPage=GoPage,
                                       UserName=username,
                                       UserPris=str(pris).split(","),
                                       SystemFlag=SystemFlag,
                                       AppVersion=APP_VERSION)
            else:
                return render_template('login.html',
                                       GoPage=GoPage,
                                       err_msg="用户名或密码错误")

    # 开始
    @App.route('/index', methods=['POST', 'GET'])
    @login_required
    def index():
        # 获取媒体数量
        ServerSucess = True
        MovieCount = 0
        SeriesCount = 0
        EpisodeCount = 0
        SongCount = 0
        MediaServerClient = MediaServer()
        media_count = MediaServerClient.get_medias_count()
        if media_count:
            MovieCount = "{:,}".format(media_count.get('MovieCount'))
            SeriesCount = "{:,}".format(media_count.get('SeriesCount'))
            SongCount = "{:,}".format(media_count.get('SongCount'))
            if media_count.get('EpisodeCount'):
                EpisodeCount = "{:,}".format(media_count.get('EpisodeCount'))
            else:
                EpisodeCount = ""
        elif media_count is None:
            ServerSucess = False

        # 获得活动日志
        Activity = MediaServerClient.get_activity_log(30)

        # 用户数量
        UserCount = MediaServerClient.get_user_count()

        # 磁盘空间
        UsedSapce = 0
        TotalSpace = 0
        FreeSpace = 0
        UsedPercent = 0
        media = config.get_config('media')
        if media:
            # 电影目录
            movie_paths = media.get('movie_path')
            if not isinstance(movie_paths, list):
                movie_paths = [movie_paths]
            movie_used, movie_total = 0, 0
            movie_space_list = []
            for movie_path in movie_paths:
                if not movie_path:
                    continue
                used, total = get_used_of_partition(movie_path)
                if "%s-%s" % (used, total) not in movie_space_list:
                    movie_space_list.append("%s-%s" % (used, total))
                    movie_used += used
                    movie_total += total
            # 电视目录
            tv_paths = media.get('tv_path')
            if not isinstance(tv_paths, list):
                tv_paths = [tv_paths]
            tv_used, tv_total = 0, 0
            tv_space_list = []
            for tv_path in tv_paths:
                if not tv_path:
                    continue
                used, total = get_used_of_partition(tv_path)
                if "%s-%s" % (used, total) not in tv_space_list:
                    tv_space_list.append("%s-%s" % (used, total))
                    tv_used += used
                    tv_total += total
            # 动漫目录
            anime_paths = media.get('anime_path')
            if not isinstance(anime_paths, list):
                anime_paths = [anime_paths]
            anime_used, anime_total = 0, 0
            anime_space_list = []
            for anime_path in anime_paths:
                if not anime_path:
                    continue
                used, total = get_used_of_partition(anime_path)
                if "%s-%s" % (used, total) not in anime_space_list:
                    anime_space_list.append("%s-%s" % (used, total))
                    anime_used += used
                    anime_total += total
            # 总空间
            TotalSpaceAry = []
            if movie_total not in TotalSpaceAry:
                TotalSpaceAry.append(movie_total)
            if tv_total not in TotalSpaceAry:
                TotalSpaceAry.append(tv_total)
            if anime_total not in TotalSpaceAry:
                TotalSpaceAry.append(anime_total)
            TotalSpace = sum(TotalSpaceAry)
            # 已使用空间
            UsedSapceAry = []
            if movie_used not in UsedSapceAry:
                UsedSapceAry.append(movie_used)
            if tv_used not in UsedSapceAry:
                UsedSapceAry.append(tv_used)
            if anime_used not in UsedSapceAry:
                UsedSapceAry.append(anime_used)
            UsedSapce = sum(UsedSapceAry)
            # 电影电视使用百分比格式化
            if TotalSpace:
                UsedPercent = "%0.1f" % ((UsedSapce / TotalSpace) * 100)
            # 总剩余空间 格式化
            FreeSpace = "{:,} TB".format(round((TotalSpace - UsedSapce) / 1024 / 1024 / 1024 / 1024, 2))
            # 总使用空间 格式化
            UsedSapce = "{:,} TB".format(round(UsedSapce / 1024 / 1024 / 1024 / 1024, 2))
            # 总空间 格式化
            TotalSpace = "{:,} TB".format(round(TotalSpace / 1024 / 1024 / 1024 / 1024, 2))

        # 查询媒体统计
        MovieChartLabels = []
        MovieNums = []
        TvChartData = {}
        TvNums = []
        AnimeNums = []
        for statistic in get_transfer_statistics():
            if statistic[0] == "电影":
                MovieChartLabels.append(statistic[1])
                MovieNums.append(statistic[2])
            else:
                if not TvChartData.get(statistic[1]):
                    TvChartData[statistic[1]] = {"tv": 0, "anime": 0}
                if statistic[0] == "电视剧":
                    TvChartData[statistic[1]]["tv"] += statistic[2]
                elif statistic[0] == "动漫":
                    TvChartData[statistic[1]]["anime"] += statistic[2]
        TvChartLabels = list(TvChartData)
        for tv_data in TvChartData.values():
            TvNums.append(tv_data.get("tv"))
            AnimeNums.append(tv_data.get("anime"))

        return render_template("index.html",
                               ServerSucess=ServerSucess,
                               MediaCount={'MovieCount': MovieCount, 'SeriesCount': SeriesCount,
                                           'SongCount': SongCount, "EpisodeCount": EpisodeCount},
                               Activitys=Activity,
                               UserCount=UserCount,
                               FreeSpace=FreeSpace,
                               TotalSpace=TotalSpace,
                               UsedSapce=UsedSapce,
                               UsedPercent=UsedPercent,
                               MovieChartLabels=MovieChartLabels,
                               TvChartLabels=TvChartLabels,
                               MovieNums=MovieNums,
                               TvNums=TvNums,
                               AnimeNums=AnimeNums
                               )

    # 影音搜索页面
    @App.route('/search', methods=['POST', 'GET'])
    @login_required
    def search():
        # 权限
        if current_user.is_authenticated:
            username = current_user.username
            pris = get_user(username).get("pris")
        else:
            pris = ""
        # 查询结果
        SearchWord = request.args.get("s")
        NeedSearch = request.args.get("f")
        res = get_search_results()
        return render_template("search.html",
                               UserPris=str(pris).split(","),
                               SearchWord=SearchWord or "",
                               NeedSearch=NeedSearch or "",
                               Count=len(res),
                               Items=res)

    # 电影订阅页面
    @App.route('/movie_rss', methods=['POST', 'GET'])
    @login_required
    def movie_rss():
        Items = get_rss_movies()
        Count = len(Items)
        return render_template("rss/movie_rss.html", Count=Count, Items=Items)

    # 电视剧订阅页面
    @App.route('/tv_rss', methods=['POST', 'GET'])
    @login_required
    def tv_rss():
        Items = get_rss_tvs()
        Count = len(Items)
        return render_template("rss/tv_rss.html", Count=Count, Items=Items)

    # 站点维护页面
    @App.route('/site', methods=['POST', 'GET'])
    @login_required
    def site():
        Sites = get_config_site()
        return render_template("setting/site.html",
                               Sites=Sites)

    # 推荐页面
    @App.route('/recommend', methods=['POST', 'GET'])
    @login_required
    def recommend():
        RecommendType = request.args.get("t")
        if RecommendType in ['hm', 'ht', 'nm', 'nt']:
            CurrentPage = request.args.get("page")
            if not CurrentPage:
                CurrentPage = 1
            else:
                CurrentPage = int(CurrentPage)

            if CurrentPage < 5:
                StartPage = 1
                EndPage = 6
            else:
                StartPage = CurrentPage - 2
                EndPage = CurrentPage + 3
            PageRange = range(StartPage, EndPage)
        else:
            PageRange = None
            CurrentPage = 0
        if RecommendType == "hm":
            # TMDB热门电影
            res_list = Media().get_tmdb_hot_movies(CurrentPage)
        elif RecommendType == "ht":
            # TMDB热门电视剧
            res_list = Media().get_tmdb_hot_tvs(CurrentPage)
        elif RecommendType == "nm":
            # TMDB最新电影
            res_list = Media().get_tmdb_new_movies(CurrentPage)
        elif RecommendType == "nt":
            # TMDB最新电视剧
            res_list = Media().get_tmdb_new_tvs(CurrentPage)
        elif RecommendType == "dbom":
            # 豆瓣正在上映
            res_list = DoubanHot().get_douban_online_movie()
        elif RecommendType == "dbhm":
            # 豆瓣热门电影
            res_list = DoubanHot().get_douban_hot_movie()
        elif RecommendType == "dbht":
            # 豆瓣热门电视剧
            res_list = DoubanHot().get_douban_hot_tv()
        elif RecommendType == "dbdh":
            # 豆瓣热门动画
            res_list = DoubanHot().get_douban_hot_anime()
        elif RecommendType == "dbnm":
            # 豆瓣最新电影
            res_list = DoubanHot().get_douban_new_movie()
        elif RecommendType == "dbnt":
            # 豆瓣最新电视剧
            res_list = DoubanHot().get_douban_new_tv()
        else:
            res_list = []

        Items = []
        TvKeys = ["%s" % key[0] for key in get_rss_tvs()]
        MovieKeys = ["%s" % key[0] for key in get_rss_movies()]
        for res in res_list:
            rid = res.get('id')
            if RecommendType in ['hm', 'nm', 'dbom', 'dbhm', 'dbnm']:
                title = res.get('title')
                if title in MovieKeys:
                    fav = 1
                else:
                    fav = 0
                date = res.get('release_date')
                if date:
                    year = date[0:4]
                else:
                    year = ''
            else:
                title = res.get('name')
                if title in TvKeys:
                    fav = 1
                else:
                    fav = 0
                date = res.get('first_air_date')
                if date:
                    year = date[0:4]
                else:
                    year = ''
            image = res.get('poster_path')
            if RecommendType in ['hm', 'nm', 'ht', 'nt']:
                image = "https://image.tmdb.org/t/p/original/%s" % image
            else:
                # 替换图片分辨率
                image = image.replace("s_ratio_poster", "m_ratio_poster")
                image = "https://images.weserv.nl/?url=%s" % image
            vote = res.get('vote_average')
            overview = res.get('overview')
            item = {'id': rid, 'title': title, 'fav': fav, 'date': date, 'vote': vote,
                    'image': image, 'overview': overview, 'year': year}
            Items.append(item)

        return render_template("recommend.html",
                               Items=Items,
                               RecommendType=RecommendType,
                               CurrentPage=CurrentPage,
                               PageRange=PageRange)

    # 正在下载页面
    @App.route('/downloading', methods=['POST', 'GET'])
    @login_required
    def download():
        DownloadCount = 0
        Client, Torrents = Downloader().pt_downloading_torrents()
        DispTorrents = []
        for torrent in Torrents:
            if Client == DownloaderType.QB:
                name = torrent.get('name')
                # 进度
                progress = round(torrent.get('progress') * 100, 1)
                if torrent.get('state') in ['pausedDL']:
                    state = "Stoped"
                    speed = "已暂停"
                else:
                    state = "Downloading"
                    dlspeed = str_filesize(torrent.get('dlspeed'))
                    upspeed = str_filesize(torrent.get('upspeed'))
                    if progress >= 100:
                        speed = "%s%sB/s %s%sB/s" % (chr(8595), dlspeed, chr(8593), upspeed)
                    else:
                        eta = str_timelong(torrent.get('eta'))
                        speed = "%s%sB/s %s%sB/s %s" % (chr(8595), dlspeed, chr(8593), upspeed, eta)
                # 主键
                key = torrent.get('hash')
            else:
                name = torrent.name
                if torrent.status in ['stopped']:
                    state = "Stoped"
                    speed = "已暂停"
                else:
                    state = "Downloading"
                    dlspeed = str_filesize(torrent.rateDownload)
                    upspeed = str_filesize(torrent.rateUpload)
                    speed = "%s%sB/s %s%sB/s" % (chr(8595), dlspeed, chr(8593), upspeed)
                # 进度
                progress = round(torrent.progress)
                # 主键
                key = torrent.id

            if not name:
                continue
            # 识别
            media_info = Media().get_media_info(name)
            if not media_info:
                continue
            if not media_info.tmdb_info:
                year = media_info.year
                if year:
                    title = "%s (%s) %s" % (media_info.get_name(), year, media_info.get_season_episode_string())
                else:
                    title = "%s %s" % (media_info.get_name(), media_info.get_season_episode_string())
            else:
                title = "%s %s" % (media_info.get_title_string(), media_info.get_season_episode_string())
            poster_path = media_info.poster_path
            torrent_info = {'id': key, 'title': title, 'speed': speed, 'image': poster_path or "", 'state': state,
                            'progress': progress}
            if torrent_info not in DispTorrents:
                DownloadCount += 1
                DispTorrents.append(torrent_info)

        return render_template("download/downloading.html",
                               DownloadCount=DownloadCount,
                               Torrents=DispTorrents)

    # 数据统计页面
    @App.route('/statistics', methods=['POST', 'GET'])
    @login_required
    def statistics():
        # 总上传下载
        TotalUpload = 0
        TotalDownload = 0
        # 站点标签及上传下载
        SiteNames = []
        SiteUploads = []
        SiteDownloads = []
        # 当前上传下载
        CurrentUpload, CurrentDownload = Downloader().get_pt_data()
        # 站点上传下载
        SiteData = Sites().get_pt_date()
        if isinstance(SiteData, dict):
            for name, data in SiteData.items():
                if not data:
                    continue
                up = data.get("upload") or 0
                dl = data.get("download") or 0
                if not up and not dl:
                    continue
                if not str(up).isdigit() or not str(dl).isdigit():
                    continue
                if name not in SiteNames:
                    SiteNames.append(name)
                    TotalUpload += int(up)
                    TotalDownload += int(dl)
                    SiteUploads.append(round(int(up)/1024/1024/1024, 1))
                    SiteDownloads.append(round(int(dl)/1024/1024/1024, 1))
        return render_template("download/statistics.html",
                               CurrentDownload=str_filesize(CurrentDownload) + "B",
                               CurrentUpload=str_filesize(CurrentUpload) + "B",
                               TotalDownload=str_filesize(TotalDownload) + "B",
                               TotalUpload=str_filesize(TotalUpload) + "B",
                               SiteDownloads=SiteDownloads,
                               SiteUploads=SiteUploads,
                               SiteNames=SiteNames)

    # 服务页面
    @App.route('/service', methods=['POST', 'GET'])
    @login_required
    def service():
        scheduler_cfg_list = []
        pt = config.get_config('pt')
        if pt:
            # RSS订阅
            pt_check_interval = pt.get('pt_check_interval')
            if pt_check_interval:
                tim_rssdownload = str(round(int(pt_check_interval) / 60)) + " 分钟"
                rss_state = 'ON'
            else:
                tim_rssdownload = ""
                rss_state = 'OFF'
            svg = '''
                <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-cloud-download" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
                     <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
                     <path d="M19 18a3.5 3.5 0 0 0 0 -7h-1a5 4.5 0 0 0 -11 -2a4.6 4.4 0 0 0 -2.1 8.4"></path>
                     <line x1="12" y1="13" x2="12" y2="22"></line>
                     <polyline points="9 19 12 22 15 19"></polyline>
                </svg>
            '''
            color = "blue"
            scheduler_cfg_list.append(
                {'name': 'RSS订阅', 'time': tim_rssdownload, 'state': rss_state, 'id': 'rssdownload', 'svg': svg,
                 'color': color})

            # PT文件转移
            pt_monitor = pt.get('pt_monitor')
            if pt_monitor:
                tim_pttransfer = str(round(PT_TRANSFER_INTERVAL / 60)) + " 分钟"
                sta_pttransfer = 'ON'
            else:
                tim_pttransfer = ""
                sta_pttransfer = 'OFF'
            svg = '''
            <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-replace" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
                 <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
                 <rect x="3" y="3" width="6" height="6" rx="1"></rect>
                 <rect x="15" y="15" width="6" height="6" rx="1"></rect>
                 <path d="M21 11v-3a2 2 0 0 0 -2 -2h-6l3 3m0 -6l-3 3"></path>
                 <path d="M3 13v3a2 2 0 0 0 2 2h6l-3 -3m0 6l3 -3"></path>
            </svg>
            '''
            color = "green"
            scheduler_cfg_list.append(
                {'name': 'PT下载转移', 'time': tim_pttransfer, 'state': sta_pttransfer, 'id': 'pttransfer', 'svg': svg,
                 'color': color})

            # PT删种
            pt_seeding_config_time = pt.get('pt_seeding_time')
            if pt_seeding_config_time and pt_seeding_config_time != '0':
                pt_seeding_time = "%s 天" % pt_seeding_config_time
                sta_autoremovetorrents = 'ON'
                svg = '''
                <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-trash" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
                     <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
                     <line x1="4" y1="7" x2="20" y2="7"></line>
                     <line x1="10" y1="11" x2="10" y2="17"></line>
                     <line x1="14" y1="11" x2="14" y2="17"></line>
                     <path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2 -2l1 -12"></path>
                     <path d="M9 7v-3a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v3"></path>
                </svg>
                '''
                color = "twitter"
                scheduler_cfg_list.append(
                    {'name': 'PT删种', 'time': pt_seeding_time, 'state': sta_autoremovetorrents,
                     'id': 'autoremovetorrents', 'svg': svg, 'color': color})

            # PT自动签到
            tim_ptsignin = pt.get('ptsignin_cron')
            if tim_ptsignin:
                if str(tim_ptsignin).find(':') == -1:
                    tim_ptsignin = "%s 小时" % tim_ptsignin
                sta_ptsignin = 'ON'
                svg = '''
                <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-user-check" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
                     <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
                     <circle cx="9" cy="7" r="4"></circle>
                     <path d="M3 21v-2a4 4 0 0 1 4 -4h4a4 4 0 0 1 4 4v2"></path>
                     <path d="M16 11l2 2l4 -4"></path>
                </svg>
                '''
                color = "facebook"
                scheduler_cfg_list.append(
                    {'name': 'PT站签到', 'time': tim_ptsignin, 'state': sta_ptsignin, 'id': 'ptsignin', 'svg': svg,
                     'color': color})

        # 目录同步
        sync = config.get_config('sync')
        if sync:
            sync_path = sync.get('sync_path')
            if sync_path:
                sta_sync = 'ON'
                svg = '''
                <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-refresh" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
                     <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
                     <path d="M20 11a8.1 8.1 0 0 0 -15.5 -2m-.5 -4v4h4"></path>
                     <path d="M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4"></path>
                </svg>
                '''
                color = "orange"
                scheduler_cfg_list.append(
                    {'name': '目录同步', 'time': '实时监控', 'state': sta_sync, 'id': 'sync', 'svg': svg, 'color': color})
        # 豆瓣同步
        douban_cfg = config.get_config('douban')
        if douban_cfg:
            interval = douban_cfg.get('interval')
            if interval:
                interval = "%s 小时" % interval
                sta_douban = "ON"
                svg = '''
                <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-bookmarks" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
                   <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
                   <path d="M13 7a2 2 0 0 1 2 2v12l-5 -3l-5 3v-12a2 2 0 0 1 2 -2h6z"></path>
                   <path d="M9.265 4a2 2 0 0 1 1.735 -1h6a2 2 0 0 1 2 2v12l-1 -.6"></path>
                </svg>
                '''
                color = "pink"
                scheduler_cfg_list.append(
                    {'name': '豆瓣收藏', 'time': interval, 'state': sta_douban, 'id': 'douban', 'svg': svg, 'color': color})

        # 实时日志
        svg = '''
        <svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-terminal" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">
           <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
           <path d="M5 7l5 5l-5 5"></path>
           <line x1="12" y1="19" x2="19" y2="19"></line>
        </svg>
        '''
        scheduler_cfg_list.append(
            {'name': '实时日志', 'time': '', 'state': 'OFF', 'id': 'logging', 'svg': svg, 'color': 'indigo'})

        return render_template("service.html",
                               Count=len(scheduler_cfg_list),
                               SchedulerTasks=scheduler_cfg_list)

    # 历史记录页面
    @App.route('/history', methods=['POST', 'GET'])
    @login_required
    def history():
        PageNum = request.args.get("pagenum")
        if not PageNum:
            PageNum = 30
        SearchStr = request.args.get("s")
        if not SearchStr:
            SearchStr = ""
        CurrentPage = request.args.get("page")
        if not CurrentPage:
            CurrentPage = 1
        else:
            CurrentPage = int(CurrentPage)
        totalCount, historys = get_transfer_history(SearchStr, CurrentPage, PageNum)
        if totalCount:
            totalCount = totalCount[0][0]
        else:
            totalCount = 0

        TotalPage = floor(totalCount / PageNum) + 1

        if TotalPage <= 5:
            StartPage = 1
            EndPage = TotalPage
        else:
            if CurrentPage <= 3:
                StartPage = 1
                EndPage = 5
            else:
                StartPage = CurrentPage - 3
                if TotalPage > CurrentPage + 3:
                    EndPage = CurrentPage + 3
                else:
                    EndPage = TotalPage

        PageRange = range(StartPage, EndPage + 1)

        return render_template("rename/history.html",
                               TotalCount=totalCount,
                               Count=len(historys),
                               Historys=historys,
                               Search=SearchStr,
                               CurrentPage=CurrentPage,
                               TotalPage=TotalPage,
                               PageRange=PageRange,
                               PageNum=PageNum)

    # 手工识别页面
    @App.route('/unidentification', methods=['POST', 'GET'])
    @login_required
    def unidentification():
        Items = []
        Records = get_transfer_unknown_paths()
        TotalCount = len(Records)
        for rec in Records:
            if not rec[1]:
                continue
            Items.append({"id": rec[0], "path": rec[1], "to": rec[2], "name": rec[1]})
        return render_template("rename/unidentification.html",
                               TotalCount=TotalCount,
                               Items=Items)

    # 基础设置页面
    @App.route('/basic', methods=['POST', 'GET'])
    @login_required
    def basic():
        proxy = config.get_config('app').get("proxies", {}).get("http")
        if proxy:
            proxy = proxy.replace("http://", "")
        return render_template("setting/basic.html", Config=config.get_config(), Proxy=proxy)

    # 目录同步页面
    @App.route('/directorysync', methods=['POST', 'GET'])
    @login_required
    def directorysync():
        sync_paths = config.get_config("sync").get("sync_path")
        SyncPaths = []
        if sync_paths:
            if isinstance(sync_paths, list):
                for sync_path in sync_paths:
                    SyncPath = {}
                    rename_flag = True
                    if sync_path.startswith("["):
                        rename_flag = False
                        sync_path = sync_path[1:-1]
                    paths = sync_path.split("|")
                    if not paths:
                        continue
                    if len(paths) > 0:
                        if not paths[0]:
                            continue
                        SyncPath['from'] = paths[0]
                    if len(paths) > 1:
                        SyncPath['to'] = paths[1]
                    if len(paths) > 2:
                        SyncPath['unknown'] = paths[2]
                    SyncPath['rename'] = rename_flag
                    SyncPaths.append(SyncPath)
            else:
                SyncPaths = [{"from": sync_paths}]
        SyncCount = len(SyncPaths)
        return render_template("setting/directorysync.html", SyncPaths=SyncPaths, SyncCount=SyncCount)

    # 豆瓣页面
    @App.route('/douban', methods=['POST', 'GET'])
    @login_required
    def douban():
        return render_template("setting/douban.html", Config=config.get_config())

    # 下载器页面
    @App.route('/downloader', methods=['POST', 'GET'])
    @login_required
    def downloader():
        # Qbittorrent
        qbittorrent = config.get_config('qbittorrent')
        save_path = qbittorrent.get("save_path")
        if isinstance(save_path, str):
            paths = save_path.split("|")
            if len(paths) > 1:
                path = paths[0]
                tag = paths[1]
            else:
                path = paths[0]
                tag = ""
            QbMovieSavePath = QbTvSavePath = QbAnimeSavePath = {"path": path, "tag": tag}
        else:
            # 电影保存目录
            movie_path = save_path.get("movie")
            if movie_path:
                paths = movie_path.split("|")
                if len(paths) > 1:
                    path = paths[0]
                    tag = paths[1]
                else:
                    path = paths[0]
                    tag = ""
            else:
                path = ""
                tag = ""
            QbMovieSavePath = {"path": path, "tag": tag}
            # 电视剧保存目录
            tv_path = save_path.get("tv")
            if tv_path:
                paths = tv_path.split("|")
                if len(paths) > 1:
                    path = paths[0]
                    tag = paths[1]
                else:
                    path = paths[0]
                    tag = ""
            else:
                path = ""
                tag = ""
            QbTvSavePath = {"path": path, "tag": tag}
            # 动漫保存目录
            anime_path = save_path.get("anime")
            if anime_path:
                paths = anime_path.split("|")
                if len(paths) > 1:
                    path = paths[0]
                    tag = paths[1]
                else:
                    path = paths[0]
                    tag = ""
            else:
                path = ""
                tag = ""
            QbAnimeSavePath = {"path": path, "tag": tag}
        contianer_path = qbittorrent.get('save_containerpath')
        if isinstance(contianer_path, str):
            QbMovieContainerPath = QbTvContainerPath = QbAnimeContainerPath = contianer_path
        else:
            if contianer_path:
                QbMovieContainerPath = contianer_path.get("movie")
                QbTvContainerPath = contianer_path.get("tv")
                QbAnimeContainerPath = contianer_path.get("anime")
            else:
                QbMovieContainerPath = QbTvContainerPath = QbAnimeContainerPath = ""

        # Transmission
        transmission = config.get_config('transmission')
        save_path = transmission.get("save_path")
        if isinstance(save_path, str):
            TrMovieSavePath = TrTvSavePath = TrAnimeSavePath = save_path
        else:
            TrMovieSavePath = save_path.get("movie")
            TrTvSavePath = save_path.get("tv")
            TrAnimeSavePath = save_path.get("anime")
        contianer_path = transmission.get('save_containerpath')
        if isinstance(contianer_path, str):
            TrMovieContainerPath = TrTvContainerPath = TrAnimeContainerPath = contianer_path
        else:
            if contianer_path:
                TrMovieContainerPath = contianer_path.get("movie")
                TrTvContainerPath = contianer_path.get("tv")
                TrAnimeContainerPath = contianer_path.get("anime")
            else:
                TrMovieContainerPath = TrTvContainerPath = TrAnimeContainerPath = ""

        return render_template("setting/downloader.html",
                               Config=config.get_config(),
                               QbMovieSavePath=QbMovieSavePath,
                               QbTvSavePath=QbTvSavePath,
                               QbAnimeSavePath=QbAnimeSavePath,
                               TrMovieSavePath=TrMovieSavePath,
                               TrTvSavePath=TrTvSavePath,
                               TrAnimeSavePath=TrAnimeSavePath,
                               QbMovieContainerPath=QbMovieContainerPath,
                               QbTvContainerPath=QbTvContainerPath,
                               QbAnimeContainerPath=QbAnimeContainerPath,
                               TrMovieContainerPath=TrMovieContainerPath,
                               TrTvContainerPath=TrTvContainerPath,
                               TrAnimeContainerPath=TrAnimeContainerPath)

    # 索引器页面
    @App.route('/indexer', methods=['POST', 'GET'])
    @login_required
    def indexer():
        return render_template("setting/indexer.html", Config=config.get_config())

    # 媒体库页面
    @App.route('/library', methods=['POST', 'GET'])
    @login_required
    def library():
        return render_template("setting/library.html", Config=config.get_config())

    # 媒体服务器页面
    @App.route('/mediaserver', methods=['POST', 'GET'])
    @login_required
    def mediaserver():
        return render_template("setting/mediaserver.html", Config=config.get_config())

    # 通知消息页面
    @App.route('/notification', methods=['POST', 'GET'])
    @login_required
    def notification():
        return render_template("setting/notification.html", Config=config.get_config())

    # 字幕设置页面
    @App.route('/subtitle', methods=['POST', 'GET'])
    @login_required
    def subtitle():
        return render_template("setting/subtitle.html", Config=config.get_config())

    # 用户管理页面
    @App.route('/users', methods=['POST', 'GET'])
    @login_required
    def users():
        user_list = get_users()
        user_count = len(user_list)
        Users = []
        for user in user_list:
            pris = str(user[3]).split(",")
            Users.append({"id": user[0], "name": user[1], "pris": pris})
        return render_template("setting/users.html", Users=Users, UserCount=user_count)

    # 事件响应
    @App.route('/do', methods=['POST'])
    @login_required
    def do():
        try:
            cmd = request.form.get("cmd")
            data = request.form.get("data")
        except Exception as e:
            return {"code": -1, "msg": str(e)}
        if data:
            data = json.loads(data)
        if cmd:
            # 启动定时服务
            if cmd == "sch":
                commands = {
                    "autoremovetorrents": AutoRemoveTorrents().run_schedule,
                    "pttransfer": PTTransfer().run_schedule,
                    "ptsignin": PTSignin().run_schedule,
                    "sync": Sync().transfer_all_sync,
                    "rssdownload": RSSDownloader().run_schedule,
                    "douban": DoubanSync().run_schedule
                }
                sch_item = data.get("item")
                if sch_item and commands.get(sch_item):
                    _thread.start_new_thread(commands.get(sch_item), ())
                return {"retmsg": "服务已启动", "item": sch_item}

            # 检索资源
            if cmd == "search":
                # 开始检索
                search_word = data.get("search_word")
                if search_word:
                    search_medias_for_web(search_word)
                return {"retcode": 0}

            # 添加下载
            if cmd == "download":
                dl_id = data.get("id")
                results = get_search_result_by_id(dl_id)
                for res in results:
                    if res[7] == "TV":
                        mtype = MediaType.TV
                    elif res[7] == "MOV":
                        mtype = MediaType.MOVIE
                    else:
                        mtype = MediaType.ANIME
                    Downloader().add_pt_torrent(res[0], mtype)
                    msg_item = MetaInfo("%s" % res[8])
                    msg_item.title = res[1]
                    msg_item.vote_average = res[5]
                    msg_item.poster_path = res[6]
                    msg_item.type = mtype
                    msg_item.description = res[9]
                    msg_item.size = res[10]
                    Message().send_download_message(SearchType.WEB, msg_item)
                return {"retcode": 0}

            # 开始下载
            if cmd == "pt_start":
                tid = data.get("id")
                if id:
                    Downloader().start_torrents(tid)
                return {"retcode": 0, "id": tid}

            # 停止下载
            if cmd == "pt_stop":
                tid = data.get("id")
                if id:
                    Downloader().stop_torrents(tid)
                return {"retcode": 0, "id": tid}

            # 删除下载
            if cmd == "pt_remove":
                tid = data.get("id")
                if id:
                    Downloader().delete_torrents(tid)
                return {"retcode": 0, "id": tid}

            # 查询具体种子的信息
            if cmd == "pt_info":
                ids = data.get("ids")
                Client, Torrents = Downloader().get_torrents(torrent_ids=ids)
                DispTorrents = []
                for torrent in Torrents:
                    if Client == DownloaderType.QB:
                        if torrent.get('state') in ['pausedDL']:
                            state = "Stoped"
                            speed = "已暂停"
                        else:
                            state = "Downloading"
                            dlspeed = str_filesize(torrent.get('dlspeed'))
                            eta = str_timelong(torrent.get('eta'))
                            upspeed = str_filesize(torrent.get('upspeed'))
                            speed = "%s%sB/s %s%sB/s %s" % (chr(8595), dlspeed, chr(8593), upspeed, eta)
                        # 进度
                        progress = round(torrent.get('progress') * 100)
                        # 主键
                        key = torrent.get('hash')
                    else:
                        if torrent.status in ['stopped']:
                            state = "Stoped"
                            speed = "已暂停"
                        else:
                            state = "Downloading"
                            dlspeed = str_filesize(torrent.rateDownload)
                            upspeed = str_filesize(torrent.rateUpload)
                            speed = "%s%sB/s %s%sB/s" % (chr(8595), dlspeed, chr(8593), upspeed)
                        # 进度
                        progress = round(torrent.progress, 1)
                        # 主键
                        key = torrent.id

                    torrent_info = {'id': key, 'speed': speed, 'state': state, 'progress': progress}
                    if torrent_info not in DispTorrents:
                        DispTorrents.append(torrent_info)

                return {"retcode": 0, "torrents": DispTorrents}

            # 删除路径
            if cmd == "del_unknown_path":
                tids = data.get("id")
                if isinstance(tids, list):
                    for tid in tids:
                        if not tid:
                            continue
                        delete_transfer_unknown(tid)
                    return {"retcode": 0}
                else:
                    retcode = delete_transfer_unknown(tids)
                    return {"retcode": retcode}

            # 手工转移
            if cmd == "rename":
                path = dest_dir = None
                logid = data.get("logid")
                if logid:
                    paths = get_transfer_path_by_id(logid)
                    if paths:
                        path = os.path.join(paths[0][0], paths[0][1])
                        dest_dir = paths[0][2]
                    else:
                        return {"retcode": -1, "retmsg": "未查询到转移日志记录"}
                else:
                    unknown_id = data.get("unknown_id")
                    if unknown_id:
                        paths = get_unknown_path_by_id(unknown_id)
                        if paths:
                            path = paths[0][0]
                            dest_dir = paths[0][1]
                        else:
                            return {"retcode": -1, "retmsg": "未查询到未识别记录"}
                if not dest_dir:
                    dest_dir = ""
                if not path:
                    return {"retcode": -1, "retmsg": "输入路径有误"}
                tmdbid = data.get("tmdb")
                title = data.get("title")
                year = data.get("year")
                mtype = data.get("type")
                season = data.get("season")
                if mtype == "TV":
                    media_type = MediaType.TV
                elif mtype == "MOV":
                    media_type = MediaType.MOVIE
                else:
                    media_type = MediaType.ANIME
                tmdb_info = Media().get_media_info_manual(media_type, title, year, tmdbid)
                if not tmdb_info:
                    return {"retcode": 1, "retmsg": "转移失败，无法查询到TMDB信息"}
                succ_flag, ret_msg = FileTransfer().transfer_media(in_from=SyncType.MAN,
                                                                   in_path=path,
                                                                   target_dir=dest_dir,
                                                                   tmdb_info=tmdb_info,
                                                                   media_type=media_type,
                                                                   season=season)
                if succ_flag:
                    if logid:
                        insert_transfer_blacklist(path)
                    else:
                        update_transfer_unknown_state(path)
                    return {"retcode": 0, "retmsg": "转移成功"}
                else:
                    return {"retcode": 2, "retmsg": ret_msg}

            # 删除识别记录及文件
            if cmd == "delete_history":
                logid = data.get('logid')
                paths = get_transfer_path_by_id(logid)
                if paths:
                    file_name = paths[0][1]
                    dest_dir = paths[0][2]
                    title = paths[0][3]
                    category = paths[0][4]
                    year = paths[0][5]
                    se = paths[0][6]
                    mtype = paths[0][7]
                    dest_path = FileTransfer().get_dest_path_by_info(dest=dest_dir, mtype=mtype, title=title,
                                                                     category=category, year=year, season=se)
                    meta_info = MetaInfo(file_name)
                    if dest_path and dest_path.find(title) != -1:
                        delete_transfer_log_by_id(logid)
                        if not meta_info.get_episode_string():
                            # 电影或者没有集数的电视剧，删除整个目录
                            try:
                                shutil.rmtree(dest_path)
                            except Exception as e:
                                log.console(str(e))
                        else:
                            # 有集数的电视剧
                            for dest_file in get_dir_files_by_ext(dest_path):
                                file_meta_info = MetaInfo(os.path.basename(dest_file))
                                if file_meta_info.get_episode_list() and set(
                                        file_meta_info.get_episode_list()).issubset(set(meta_info.get_episode_list())):
                                    try:
                                        shutil.rmtree(dest_file)
                                    except Exception as e:
                                        log.console(str(e))
                return {"retcode": 0}

            # 查询实时日志
            if cmd == "logging":
                if LOG_QUEUE:
                    return {"text": "<br/>".join(list(LOG_QUEUE))}
                return {"text": ""}

            # 检查新版本
            if cmd == "version":
                version = ""
                info = ""
                code = 0
                try:
                    response = requests.get("https://api.github.com/repos/jxxghp/nas-tools/releases/latest", timeout=10,
                                            proxies=config.get_proxies())
                    if response:
                        ver_json = response.json()
                        version = ver_json["tag_name"]
                        info = f'<a href="{ver_json["html_url"]}" target="_blank">{version}</a>'
                except Exception as e:
                    log.console(str(e))
                    code = -1
                return {"code": code, "version": version, "info": info}

            # 查询实时日志
            if cmd == "update_site":
                tid = data.get('site_id')
                name = data.get('site_name')
                site_pri = data.get('site_pri')
                rssurl = data.get('site_rssurl')
                signurl = data.get('site_signurl')
                cookie = data.get('site_cookie')
                include = data.get('site_include')
                exclude = data.get('site_exclude')
                note = data.get('site_note')
                size = data.get('site_size')
                if tid:
                    ret = update_config_site(tid=tid,
                                             name=name,
                                             site_pri=site_pri,
                                             rssurl=rssurl,
                                             signurl=signurl,
                                             cookie=cookie,
                                             include=include,
                                             exclude=exclude,
                                             size=size,
                                             note=note)
                else:
                    ret = insert_config_site(name=name,
                                             site_pri=site_pri,
                                             rssurl=rssurl,
                                             signurl=signurl,
                                             cookie=cookie,
                                             include=include,
                                             exclude=exclude,
                                             size=size,
                                             note=note)
                return {"code": ret}

            # 查询单个站点信息
            if cmd == "get_site":
                tid = data.get("id")
                if tid:
                    ret = get_site_by_id(tid)
                else:
                    ret = []
                return {"code": 0, "site": ret}

            # 删除单个站点信息
            if cmd == "del_site":
                tid = data.get("id")
                if tid:
                    ret = delete_config_site(tid)
                    return {"code": ret}
                else:
                    return {"code": 0}

            # 查询搜索过滤规则
            if cmd == "get_search_rule":
                ret = get_config_search_rule()
                return {"code": 0, "rule": ret}

            # 更新搜索过滤规则
            if cmd == "update_search_rule":
                include = data.get('search_include')
                exclude = data.get('search_exclude')
                note = data.get('search_note')
                size = data.get('search_size')
                ret = update_config_search_rule(include=include, exclude=exclude, note=note, size=size)
                return {"code": ret}

            # 查询RSS全局过滤规则
            if cmd == "get_rss_rule":
                ret = get_config_rss_rule()
                return {"code": 0, "rule": ret}

            # 更新搜索过滤规则
            if cmd == "update_rss_rule":
                note = data.get('rss_note')
                ret = update_config_rss_rule(note=note)
                return {"code": ret}

            # 重启
            if cmd == "restart":
                # 停止服务
                stop_service()
                # 退出主进程
                shutdown_server()

            # 更新
            if cmd == "update_system":
                # 停止服务
                stop_service()
                # 安装依赖
                call(['pip', 'install', '-r', '/nas-tools/requirements.txt', ])
                # 升级
                call(['git', 'pull'])
                # 退出主进程
                shutdown_server()

            # 注销
            if cmd == "logout":
                logout_user()
                return {"code": 0}

            # 更新配置信息
            if cmd == "update_config":
                cfg = config.get_config()
                cfgs = dict(data).items()
                # 重载配置标志
                config_test = False
                scheduler_reload = False
                jellyfin_reload = False
                plex_reload = False
                qbittorrent_reload = False
                transmission_reload = False
                wechat_reload = False
                telegram_reload = False
                # 修改配置
                for key, value in cfgs:
                    if key == "test" and value:
                        config_test = True
                        continue
                    # 生效配置
                    cfg = set_config_value(cfg, key, value)
                    if key in ['pt.ptsignin_cron', 'pt.pt_monitor', 'pt.pt_check_interval', 'pt.pt_seeding_time',
                               'douban.interval']:
                        scheduler_reload = True
                    if key.startswith("jellyfin"):
                        jellyfin_reload = True
                    if key.startswith("plex"):
                        plex_reload = True
                    if key.startswith("qbittorrent"):
                        qbittorrent_reload = True
                    if key.startswith("transmission"):
                        transmission_reload = True
                    if key.startswith("message.telegram"):
                        telegram_reload = True
                    if key.startswith("message.wechat"):
                        wechat_reload = True
                # 保存配置
                if not config_test:
                    config.save_config(cfg)
                # 重启定时服务
                if scheduler_reload:
                    Scheduler().init_config()
                    restart_scheduler()
                # 重载Jellyfin
                if jellyfin_reload:
                    Jellyfin().init_config()
                # 重载Plex
                if plex_reload:
                    Plex().init_config()
                # 重载qbittorrent
                if qbittorrent_reload:
                    Qbittorrent().init_config()
                # 重载transmission
                if transmission_reload:
                    Transmission().init_config()
                # 重载wechat
                if wechat_reload:
                    WeChat().init_config()
                # 重载telegram
                if telegram_reload:
                    Telegram().init_config()

                return {"code": 0}

            # 维护媒体库目录
            if cmd == "update_directory":
                cfg = set_config_directory(config.get_config(), data.get("oper"), data.get("key"), data.get("value"))
                # 保存配置
                config.save_config(cfg)
                if data.get("key") == "sync.sync_path":
                    # 生效配置
                    Sync().init_config()
                    # 重启目录同步服务
                    restart_monitor()
                return {"code": 0}

            # 移除RSS订阅
            if cmd == "remove_rss_media":
                name = data.get("name")
                mtype = data.get("type")
                year = data.get("year")
                season = data.get("season")
                if name and mtype:
                    if mtype in ['nm', 'hm', 'dbom', 'dbhm', 'dbnm', 'MOV']:
                        delete_rss_movie(name, year)
                    else:
                        delete_rss_tv(name, year, season)
                return {"code": 0}

            # 添加RSS订阅
            if cmd == "add_rss_media":
                name = data.get("name")
                mtype = data.get("type")
                year = data.get("year")
                season = data.get("season")
                if name and mtype:
                    if mtype in ['nm', 'hm', 'dbom', 'dbhm', 'dbnm', 'MOV']:
                        mtype = MediaType.MOVIE
                    else:
                        mtype = MediaType.TV
                code, msg, media_info = add_rss_subscribe(mtype, name, year, season)
                return {"code": code, "msg": msg}

            # 未识别的重新识别
            if cmd == "re_identification":
                path = dest_dir = None
                unknown_id = data.get("unknown_id")
                if unknown_id:
                    paths = get_unknown_path_by_id(unknown_id)
                    if paths:
                        path = paths[0][0]
                        dest_dir = paths[0][1]
                    else:
                        return {"retcode": -1, "retmsg": "未查询到未识别记录"}
                if not dest_dir:
                    dest_dir = ""
                if not path:
                    return {"retcode": -1, "retmsg": "未识别路径有误"}
                succ_flag, ret_msg = FileTransfer().transfer_media(in_from=SyncType.MAN,
                                                                   in_path=path,
                                                                   target_dir=dest_dir)
                if succ_flag:
                    update_transfer_unknown_state(path)
                    return {"retcode": 0, "retmsg": "转移成功"}
                else:
                    return {"retcode": 2, "retmsg": ret_msg}

            # 根据TMDB查询媒体信息
            if cmd == "media_info":
                tmdbid = data.get("id")
                mtype = data.get("type")
                title = data.get("title")
                year = data.get("year")
                doubanid = data.get("doubanid")
                if mtype in ['hm', 'nm', 'dbom', 'dbhm', 'dbnm', 'MOV']:
                    media_type = MediaType.MOVIE
                else:
                    media_type = MediaType.TV

                if mtype in ['hm', 'nm']:
                    link_url = "https://www.themoviedb.org/movie/%s" % tmdbid
                elif mtype in ['ht', 'nt']:
                    link_url = "https://www.themoviedb.org/tv/%s" % tmdbid
                else:
                    link_url = "https://movie.douban.com/subject/%s" % doubanid

                tmdb_info = Media().get_media_info_manual(media_type, title, year, tmdbid)
                if not tmdb_info:
                    return {"code": 1, "retmsg": "无法查询到TMDB信息", "link_url": link_url}

                overview = tmdb_info.get("overview")
                poster_path = "https://image.tmdb.org/t/p/w500%s" % tmdb_info.get('poster_path')
                if media_type == MediaType.MOVIE:
                    if doubanid:
                        douban_info = Douban().movie_detail(doubanid)
                        overview = douban_info.get("intro")
                        poster_path = "https://images.weserv.nl/?url=%s" % douban_info.get("cover_url")

                    return {
                        "code": 0,
                        "id": tmdb_info.get('id'),
                        "title": tmdb_info.get('title'),
                        "vote_average": tmdb_info.get("vote_average"),
                        "poster_path": poster_path,
                        "release_date": tmdb_info.get('release_date'),
                        "year": tmdb_info.get('release_date')[0:4] if tmdb_info.get('release_date') else "",
                        "overview": overview,
                        "link_url": link_url
                    }
                else:
                    if doubanid:
                        douban_info = Douban().tv_detail(doubanid)
                        overview = douban_info.get("intro")
                        poster_path = "https://images.weserv.nl/?url=%s" % douban_info.get("cover_url")

                    return {
                        "code": 0,
                        "id": tmdb_info.get('id'),
                        "title": tmdb_info.get('name'),
                        "vote_average": tmdb_info.get("vote_average"),
                        "poster_path": poster_path,
                        "first_air_date": tmdb_info.get('first_air_date'),
                        "year": tmdb_info.get('first_air_date')[0:4] if tmdb_info.get('first_air_date') else "",
                        "overview": overview,
                        "link_url": link_url
                    }

            # 测试连通性
            if cmd == "test_connection":
                # 支持两种传入方式：命令数组或单个命令，单个命令时xx|xx模式解析为模块和类，进行动态引入
                command = data.get("command")
                ret = None
                if command:
                    try:
                        if isinstance(command, list):
                            for cmd_str in command:
                                ret = eval(cmd_str)
                                if not ret:
                                    break
                        else:
                            if command.find("|") != -1:
                                module = command.split("|")[0]
                                class_name = command.split("|")[1]
                                ret = getattr(importlib.import_module(module), class_name)().get_status()
                            else:
                                ret = eval(command)
                        # 重载配置
                        config.init_config()
                    except Exception as e:
                        ret = None
                        print(str(e))
                    return {"code": 0 if ret else 1}
                return {"code": 0}

            # 用户管理
            if cmd == "user_manager":
                oper = data.get("oper")
                name = data.get("name")
                if oper == "add":
                    password = generate_password_hash(str(data.get("password")))
                    pris = data.get("pris")
                    if isinstance(pris, list):
                        pris = ",".join(pris)
                    ret = insert_user(name, password, pris)
                else:
                    ret = delete_user(name)
                return {"code": ret}

            # 重新搜索RSS
            if cmd == "refresh_rss":
                mtype = data.get("type")
                rssid = data.get("rssid")
                if mtype == "MOV":
                    _thread.start_new_thread(Rss().rsssearch_movie, (rssid,))
                else:
                    _thread.start_new_thread(Rss().rsssearch_tv, (rssid,))
                return {"code": 0}

            # 刷新首页消息中心
            if cmd == "refresh_message":
                lst_time = data.get("lst_time")
                messages = get_system_messages(lst_time=lst_time)
                return {"code": 0, "message": messages}

    # 响应企业微信消息
    @App.route('/wechat', methods=['GET', 'POST'])
    def wechat():
        message = config.get_config('message')
        sToken = message.get('wechat', {}).get('Token')
        sEncodingAESKey = message.get('wechat', {}).get('EncodingAESKey')
        sCorpID = message.get('wechat', {}).get('corpid')
        if not sToken or not sEncodingAESKey or not sCorpID:
            return
        wxcpt = WXBizMsgCrypt(sToken, sEncodingAESKey, sCorpID)
        sVerifyMsgSig = request.args.get("msg_signature")
        sVerifyTimeStamp = request.args.get("timestamp")
        sVerifyNonce = request.args.get("nonce")

        if request.method == 'GET':
            sVerifyEchoStr = request.args.get("echostr")
            log.debug("收到微信验证请求: echostr= %s" % sVerifyEchoStr)
            ret, sEchoStr = wxcpt.VerifyURL(sVerifyMsgSig, sVerifyTimeStamp, sVerifyNonce, sVerifyEchoStr)
            if ret != 0:
                log.error("微信请求验证失败 VerifyURL ret: %s" % str(ret))
            # 验证URL成功，将sEchoStr返回给企业号
            return sEchoStr
        else:
            sReqData = request.data
            log.debug("收到微信消息：" + str(sReqData))
            ret, sMsg = wxcpt.DecryptMsg(sReqData, sVerifyMsgSig, sVerifyTimeStamp, sVerifyNonce)
            if ret != 0:
                log.error("解密微信消息失败 DecryptMsg ret：%s" % str(ret))
            xml_tree = ETree.fromstring(sMsg)
            reponse_text = ""
            try:
                msg_type = xml_tree.find("MsgType").text
                user_id = xml_tree.find("FromUserName").text
                if msg_type == "event":
                    event_key = xml_tree.find("EventKey").text
                    log.info("点击菜单：" + event_key)
                    content = WECHAT_MENU[event_key.split('#')[2]]
                else:
                    content = xml_tree.find("Content").text
                    log.info("消息内容：" + content)
                    reponse_text = content
            except Exception as err:
                log.error("发生错误：%s" % str(err))
                return make_response("", 200)
            # 处理消息内容
            content = content.strip()
            if content:
                handle_message_job(content, SearchType.WX, user_id)
            return make_response(reponse_text, 200)

    # Emby消息通知
    @App.route('/jellyfin', methods=['POST'])
    @App.route('/emby', methods=['POST'])
    def webhook():
        if not MediaServer().webhook_allow_access(request.remote_addr):
            log.warn(f"非法IP地址的媒体消息通知 {request.remote_addr}")
            return 'Success'
        request_json = json.loads(request.form.get('data', {}))
        # print(str(request_json))
        event = WebhookEvent(request_json)
        event.report_to_discord()
        return 'Success'

    # Telegram消息
    @App.route('/telegram', methods=['POST', 'GET'])
    def telegram():
        msg_json = request.get_json()
        if msg_json:
            message = msg_json.get("message", {})
            text = message.get("text")
            user_id = message.get("from", {}).get("id")
            if text:
                handle_message_job(text, SearchType.TG, user_id)
        return 'ok'

    # 处理消息事件
    def handle_message_job(msg, in_from=SearchType.OT, user_id=None):
        if not msg:
            return
        commands = {
            "/ptr": {"func": AutoRemoveTorrents().run_schedule, "desp": "PT删种"},
            "/ptt": {"func": PTTransfer().run_schedule, "desp": "PT下载转移"},
            "/pts": {"func": PTSignin().run_schedule, "desp": "PT站签到"},
            "/rst": {"func": Sync().transfer_all_sync, "desp": "监控目录全量同步"},
            "/rss": {"func": RSSDownloader().run_schedule, "desp": "RSS订阅"},
            "/db": {"func": DoubanSync().run_schedule, "desp": "豆瓣收藏同步"}
        }
        command = commands.get(msg)
        if command:
            # 检查用户权限
            if in_from == SearchType.TG and user_id:
                if str(user_id) != Telegram().get_admin_user():
                    Message().send_channel_msg(channel=in_from, title="错误：只有管理员才有权限执行此命令")
                    return
            # 启动服务
            _thread.start_new_thread(command.get("func"), ())
            Message().send_channel_msg(channel=in_from, title="已启动：%s" % command.get("desp"))
        elif msg.startswith("订阅"):
            # 添加订阅
            _thread.start_new_thread(add_rss_substribe_from_string, (msg, in_from, user_id,))
        else:
            # PT检索
            _thread.start_new_thread(Searcher().search_one_media, (msg, in_from, user_id,))

    # 根据Key设置配置值
    def set_config_value(cfg, cfg_key, cfg_value):
        # 密码
        if cfg_key == "app.login_password":
            if cfg_value and not cfg_value.startswith("[hash]"):
                cfg['app']['login_password'] = "[hash]%s" % generate_password_hash(cfg_value)
            else:
                cfg['app']['login_password'] = cfg_value or "password"
            return cfg
        # 代理
        if cfg_key == "app.proxies":
            if cfg_value:
                if not cfg_value.startswith("http") and not cfg_value.startswith("sock"):
                    cfg['app']['proxies'] = {"https": "http://%s" % cfg_value, "http": "http://%s" % cfg_value}
                else:
                    cfg['app']['proxies'] = {"https": "%s" % cfg_value, "http": "%s" % cfg_value}
            else:
                cfg['app']['proxies'] = {"https": None, "http": None}
            return cfg
        # 文件转移模式
        if cfg_key == "app.rmt_mode":
            cfg['sync']['sync_mod'] = cfg_value
            cfg['pt']['rmt_mode'] = cfg_value
            return cfg
        # 豆瓣用户列表
        if cfg_key == "douban.users":
            vals = cfg_value.split(",")
            cfg['douban']['users'] = vals
            return cfg
        # 索引器
        if cfg_key == "jackett.indexers":
            vals = cfg_value.split("\n")
            cfg['jackett']['indexers'] = vals
            return cfg
        # 最大支持三层赋值
        keys = cfg_key.split(".")
        if keys:
            if len(keys) == 1:
                cfg[keys[0]] = cfg_value
            elif len(keys) == 2:
                if not cfg.get(keys[0]):
                    cfg[keys[0]] = {}
                cfg[keys[0]][keys[1]] = cfg_value
            elif len(keys) == 3:
                if cfg.get(keys[0]):
                    if not cfg[keys[0]].get(keys[1]) or isinstance(cfg[keys[0]][keys[1]], str):
                        cfg[keys[0]][keys[1]] = {}
                    cfg[keys[0]][keys[1]][keys[2]] = cfg_value
                else:
                    cfg[keys[0]] = {}
                    cfg[keys[0]][keys[1]] = {}
                    cfg[keys[0]][keys[1]][keys[2]] = cfg_value

        return cfg

    # 更新目录数据
    def set_config_directory(cfg, oper, cfg_key, cfg_value):
        # 最大支持二层赋值
        keys = cfg_key.split(".")
        if keys:
            if len(keys) == 1:
                if cfg.get(keys[0]):
                    if not isinstance(cfg[keys[0]], list):
                        cfg[keys[0]] = [cfg[keys[0]]]
                    if oper == "add":
                        cfg[keys[0]].append(cfg_value)
                    else:
                        cfg[keys[0]].remove(cfg_value)
                        if not cfg[keys[0]]:
                            cfg[keys[0]] = None
                else:
                    cfg[keys[0]] = cfg_value
            elif len(keys) == 2:
                if cfg.get(keys[0]):
                    if not cfg[keys[0]].get(keys[1]):
                        cfg[keys[0]][keys[1]] = []
                    if not isinstance(cfg[keys[0]][keys[1]], list):
                        cfg[keys[0]][keys[1]] = [cfg[keys[0]][keys[1]]]
                    if oper == "add":
                        cfg[keys[0]][keys[1]].append(cfg_value)
                    else:
                        cfg[keys[0]][keys[1]].remove(cfg_value)
                        if not cfg[keys[0]][keys[1]]:
                            cfg[keys[0]][keys[1]] = None
                else:
                    cfg[keys[0]] = {}
                    cfg[keys[0]][keys[1]] = cfg_value
        return cfg

    return App
