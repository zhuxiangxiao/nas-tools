import os
import signal
import sys
import warnings
from pyvirtualdisplay import Display

# 添加第三方库入口
with open(os.path.join(os.path.dirname(__file__),
                       "third_party.txt"), "r") as f:
    third_party = f.readlines()
    for third_party_lib in third_party:
        sys.path.append(os.path.join(os.path.dirname(__file__),
                                     "third_party",
                                     third_party_lib.strip()).replace("\\", "/"))

# 运行环境判断
is_windows_exe = getattr(sys, 'frozen', False) and (os.name == "nt")
if is_windows_exe:
    # 托盘相关库
    import threading
    from windows.trayicon import trayicon

    # 初始化环境变量
    os.environ["NASTOOL_CONFIG"] = os.path.join(os.path.dirname(sys.executable),
                                                "config",
                                                "config.yaml").replace("\\", "/")
    os.environ["NASTOOL_LOG"] = os.path.join(os.path.dirname(sys.executable),
                                             "config",
                                             "logs").replace("\\", "/")
    try:
        config_dir = os.path.join(os.path.dirname(sys.executable),
                                  "config").replace("\\", "/")
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        feapder_tmpdir = os.path.join(os.path.dirname(__file__),
                                      "feapder",
                                      "network",
                                      "proxy_file").replace("\\", "/")
        if not os.path.exists(feapder_tmpdir):
            os.makedirs(feapder_tmpdir)
    except Exception as err:
        print(str(err))

# 启动虚拟显示
is_docker = os.path.exists('/.dockerenv')
if is_docker:
    try:
        display = Display(visible=False, size=(1920, 1080))
        display.start()
        os.environ['NASTOOL_DISPLAY'] = 'YES'
    except Exception as err:
        print(str(err))
else:
    display = None

from config import CONFIG
import log
from web.main import App
from app.brushtask import BrushTask
from app.db import init_db, update_db
from app.helper import IndexerHelper
from app.rsschecker import RssChecker
from app.scheduler import run_scheduler
from app.sync import run_monitor
from check_config import update_config, check_config
from version import APP_VERSION

warnings.filterwarnings('ignore')


def sigal_handler(num, stack):
    """
    信号处理
    """
    if is_docker:
        log.warn('捕捉到退出信号：%s，开始退出...' % num)
        # 停止虚拟显示
        if display:
            display.stop()
        # 退出主进程
        sys.exit()


def get_run_config(cfg):
    """
    获取运行配置
    """
    _web_host = "::"
    _web_port = 3000
    _ssl_cert = None
    _ssl_key = None

    app_conf = cfg.get_config('app')
    if app_conf:
        if app_conf.get("web_host"):
            _web_host = app_conf.get("web_host").replace('[', '').replace(']', '')
        _web_port = int(app_conf.get('web_port')) if str(app_conf.get('web_port', '')).isdigit() else 3000
        _ssl_cert = app_conf.get('ssl_cert')
        _ssl_key = app_conf.get('ssl_key')

    app_arg = dict(host=_web_host, port=_web_port, debug=False, threaded=True, use_reloader=False)
    if _ssl_cert:
        app_arg['ssl_context'] = (_ssl_cert, _ssl_key)
    return app_arg


# 退出事件
signal.signal(signal.SIGINT, sigal_handler)
signal.signal(signal.SIGTERM, sigal_handler)


# 初始化
def init_system():
    # 配置
    log.console('NAStool 当前版本号：%s' % APP_VERSION)
    # 数据库初始化
    init_db()
    # 数据库更新
    update_db()
    # 升级配置文件
    update_config()
    # 检查配置文件
    check_config()


# 启动附属服务
def start_service():
    log.console("开始启动进程...")
    # 启动定时服务
    run_scheduler()
    # 启动监控服务
    run_monitor()
    # 启动刷流服务
    BrushTask()
    # 启动自定义订阅服务
    RssChecker()
    # 加载索引器配置
    IndexerHelper()


# 系统初始化
init_system()

# 启动服务
start_service()


# 本地运行
if __name__ == '__main__':
    # Windows启动托盘
    if is_windows_exe:
        homepage = CONFIG.get_config('app').get('domain')
        if not homepage:
            homepage = "http://localhost:%s" % str(CONFIG.get_config('app').get('web_port'))
        log_path = os.environ.get("NASTOOL_LOG")


        def traystart():
            trayicon(homepage, log_path)


        if len(os.popen("tasklist| findstr %s" % os.path.basename(sys.executable), 'r').read().splitlines()) <= 2:
            p1 = threading.Thread(target=traystart, daemon=True)
            p1.start()

    # gunicorn 启动
    App.run(**get_run_config(CONFIG))
