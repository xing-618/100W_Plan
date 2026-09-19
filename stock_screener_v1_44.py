try:
    import baostock as bs
except ImportError as exc:
    raise RuntimeError('缺少 baostock，请先在当前虚拟环境执行：pip install baostock') from exc
import pandas as pd
import numpy as np
import datetime as dt
import sqlite3
import json
import requests
import os
import re
import sys
import subprocess
import urllib.parse
import urllib.request
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont
import warnings
warnings.filterwarnings('ignore')
import logging

# Windows 中文字体：Tkinter 界面与 Matplotlib 统一使用微软雅黑，避免中文显示成方块。
UI_FONT = ('Microsoft YaHei', 9)
UI_FONT_BOLD = ('Microsoft YaHei', 9, 'bold')

import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
# Windows常用中文字体优先，避免图表中文乱码

# 选择当前机器实际存在的中文字体，避免 matplotlib 反复输出 findfont 警告。
def _pick_matplotlib_chinese_font():
    candidates = ['Microsoft YaHei', 'SimHei', 'SimSun', 'Arial Unicode MS', 'Noto Sans CJK SC', 'DejaVu Sans']
    for name in candidates:
        try:
            fm.findfont(fm.FontProperties(family=name), fallback_to_default=False)
            return name
        except Exception:
            continue
    return 'DejaVu Sans'

MPL_CHINESE_FONT = _pick_matplotlib_chinese_font()
plt.rcParams['font.sans-serif'] = [MPL_CHINESE_FONT]
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['font.family'] = MPL_CHINESE_FONT
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import mplfinance as mpf

try:
    import tushare as ts
except ImportError:
    ts = None

try:
    import akshare as ak
except ImportError:
    ak = None

AKSHARE_TARGET_VERSION = '1.18.81'  # Python 3.10兼容版本；新版AKShare目前要求Python 3.11+。


def _ensure_pip_available():
    """确保当前解释器拥有pip；失败则返回False，不阻塞主程序。"""
    try:
        subprocess.run(
            [sys.executable, '-m', 'pip', '--version'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=8, check=True
        )
        return True
    except Exception:
        try:
            import ensurepip
            ensurepip.bootstrap(upgrade=False)
            subprocess.run(
                [sys.executable, '-m', 'pip', '--version'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=True
            )
            return True
        except Exception:
            return False


def ensure_akshare_installed(quiet=False):
    """按当前Python版本自动尝试安装AKShare；网络/证书失败时不让主程序崩溃。"""
    global ak
    if ak is not None:
        return True, f'AKShare已安装：{getattr(ak, "__version__", "未知版本")}'

    if sys.version_info[:2] >= (3, 11):
        target = 'akshare'
    elif sys.version_info[:2] >= (3, 9):
        target = f'akshare=={AKSHARE_TARGET_VERSION}'
    else:
        return False, f'当前Python {sys.version.split()[0]} 太旧，AKShare需要Python 3.9+。'

    if not _ensure_pip_available():
        return False, '当前Python环境没有可用的pip，且自动修复pip失败。'

    indexes = []
    custom_index = os.getenv('PIP_INDEX_URL', '').strip()
    if custom_index:
        indexes.append(custom_index)
    indexes.extend([
        'https://pypi.org/simple',
        'https://mirrors.aliyun.com/pypi/simple/',
        'http://mirrors.aliyun.com/pypi/simple/',
    ])

    errors = []
    for index in indexes:
        cmd = [
            sys.executable, '-m', 'pip', 'install', target,
            '-i', index, '--disable-pip-version-check',
            '--no-input', '--timeout', '10', '--retries', '1',
        ]
        if index.startswith('http://'):
            host = urllib.parse.urlparse(index).hostname
            if host:
                cmd.extend(['--trusted-host', host])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                try:
                    import importlib
                    ak = importlib.import_module('akshare')
                    return True, f'AKShare安装成功：{getattr(ak, "__version__", "未知版本")}'
                except Exception as exc:
                    errors.append(f'{index}：安装成功但导入失败：{exc}')
            else:
                tail = (proc.stderr or proc.stdout or '').strip().replace('\n', ' ')
                errors.append(f'{index}：{tail[-240:]}')
        except Exception as exc:
            errors.append(f'{index}：{exc}')

    return False, 'AKShare自动安装失败。' + ('；'.join(errors[-3:]) if errors else '')


# ============================================================
# A股短线研究工具 V1.44：MACD阶段识别 + 多因子人工复核 + 行业/市值修复
#
# V1.2 目标：
# 1. 收盘复盘：只使用已完成交易日的日线数据。
# 2. 盘中采样：使用5分钟线独立保存，每次运行都形成一个采样快照。
# 3. 自选股查询：输入 600888、000917 或用顿号/逗号/空格分隔。
# 4. 策略关注度：在原有硬筛选通过后，对候选进行0~100分排序。
# 5. K线图：日K + 成交量 + MACD 三联展示。
# 6. 日线数据与盘中数据完全分表，不互相覆盖。
# 7. 不做真实自动交易，不把“推荐度”解释成买入保证。
# ============================================================

APP_TITLE = 'A股短线研究工具 V1.44'
DB_PATH = './stock_data.db'
STOCK_PREFIXES = ('60', '00')
KNOWN_STOCK_NAME_FIXES = {'000917': '电广传媒'}
SECTOR_COUNT = 4                     # 仅表示下载批次，不代表真实行业板块
BATCH_SAVE_SIZE = 20
REQUEST_DELAY = 0.05
MAX_RETRIES = 2
BS_RECONNECT_EVERY = 60       # BaoStock连续处理多少只后主动刷新一次连接
BS_RECONNECT_WAIT = 1.2      # 网络异常重连前等待秒数
BS_BACKOFF_MAX = 6.0        # 单只股票重试最大等待秒数
LOAD_CALENDAR_DAYS = 420
LOOKBACK_TRADING_DAYS = 250
CHART_DAYS = 250
INTRADAY_FREQUENCY = '5'

MARKET_OPEN = dt.time(9, 30)
MORNING_CLOSE = dt.time(11, 30)
AFTERNOON_OPEN = dt.time(13, 0)
MARKET_CLOSE = dt.time(15, 0)
CLOSE_REVIEW_READY_TIME = dt.time(15, 5)
PROGRESS_UPDATE_INTERVAL = 0.08

LOG_PATH = './stock_screener.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('stock_screener')

# 数据源：auto / baostock / sina / tushare / akshare
# Tushare 令牌建议放环境变量 TUSHARE_TOKEN，避免写死在代码里。
DATA_SOURCE_MODE = os.getenv('ASTOCK_DATA_SOURCE', 'auto').lower()
TUSHARE_TOKEN = os.getenv('TUSHARE_TOKEN', '').strip()

DATA_SOURCE_LABELS = {
    'auto': '自动：BaoStock优先，新浪兜底',
    'baostock': 'BaoStock',
    'sina': '新浪历史日线',
    'tushare': 'Tushare',
    'akshare': 'AkShare（备用）',
}

if DATA_SOURCE_MODE not in DATA_SOURCE_LABELS:
    DATA_SOURCE_MODE = 'auto'

DEFAULT_PARAMS = {
    'max_price': 10.0,
    'min_price': 2.0,
    'min_limit_ups': 5,
    'recent_days': 10,
    'max_daily_gain': 5.0,
    'limit_up_threshold': 9.8,
    'min_volume_ratio': 0.0,
    'min_amount': 5_000_000,
    'shareholder_enabled': False,
    'shareholder_weight': 8.0,
    'market_environment_enabled': True,
    'market_weight': 12.0,
    'relative_strength_weight': 10.0,
    'market_cap_weight': 8.0,
    't1_high_open_pct': 4.0,
    't1_low_open_pct': -2.0,
    # V1.42：默认要求死叉至少连续两日修复；单日修复仅在改善幅度/动能确认较强时放行。
    'macd_repair_require_2d': True,
    'macd_one_day_strong_shrink_pct': 4.0,
    'macd_weak_market_score': 40.0,
    'macd_weak_market_min_rel5': 0.0,
    # V1.42：避免把推荐日已经明显拉升的股票排到前面。
    'signal_day_chase_warn_pct': 2.5,
    'signal_day_chase_hard_pct': 4.0,
    'signal_day_drop_hard_pct': -4.0,
    # V1.44：二级优选层——保留全量候选池，独立输出极度推荐 Top 5。
    'extreme_pick_count': 5,
    # V1.44 极度推荐：对应个人看盘框架，而不是单一指标排名。
    'extreme_macd_weight': 24.0,
    'extreme_ma5_weight': 10.0,
    'extreme_kdj_weight': 7.0,
    'extreme_rsi_weight': 7.0,
    'extreme_volume_weight': 10.0,
    'extreme_position_weight': 8.0,
    'extreme_market_cap_weight': 6.0,
    'extreme_turnover_weight': 4.0,
    'extreme_limit_gene_weight': 18.0,
    'extreme_context_weight': 6.0,
    'macd_tangle_ratio': 0.35,
}


# ==================== 工具 ====================
class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind('<Enter>', self.show_tip)
        widget.bind('<Leave>', self.hide_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text:
            return
        try:
            x, y, _, _ = self.widget.bbox('insert')
        except Exception:
            x, y = 0, 0
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{x}+{y}')
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background='#ffffe0', relief=tk.SOLID,
                         borderwidth=1, font=('tahoma', '8', 'normal'))
        label.pack(ipadx=1)

    def hide_tip(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


def normalize_code(code):
    if pd.isna(code):
        return ''
    text = str(code).strip()
    if '.' in text:
        text = text.split('.')[-1]
    return text.zfill(6)


def parse_date(text):
    return dt.datetime.strptime(str(text).strip(), '%Y-%m-%d')


def today_str():
    return dt.datetime.now().strftime('%Y-%m-%d')


def now_time():
    return dt.datetime.now().time()


def format_baostock_code(code):
    code = normalize_code(code)
    if code.startswith('6'):
        return f'sh.{code}'
    if code.startswith(('0', '3')):
        return f'sz.{code}'
    return code


def safe_float(value):
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def parse_codes(text):
    # 支持 600888、000917，也支持顿号、逗号、空格、换行、分号。
    text = str(text or '')
    for sep in ['、', ',', '，', ';', '；', '\n', '\t']:
        text = text.replace(sep, ' ')
    result = []
    for part in text.split():
        code = normalize_code(part)
        if len(code) == 6 and code.isdigit():
            result.append(code)
    return list(dict.fromkeys(result))


def is_before_close_for_today(target_date):
    return target_date == today_str() and now_time() < CLOSE_REVIEW_READY_TIME


def latest_completed_local_date(data_manager, before_today=True):
    with sqlite3.connect(data_manager.db_path) as conn:
        if before_today:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM daily_data WHERE trade_date<? AND adjust_flag='raw'",
                (today_str(),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM daily_data WHERE adjust_flag='raw'"
            ).fetchone()
    return row[0] if row and row[0] else None


def trading_session_open():
    t = now_time()
    return (MARKET_OPEN <= t <= MORNING_CLOSE) or (AFTERNOON_OPEN <= t <= MARKET_CLOSE)


def strict_score_reason(score):
    if score >= 85:
        return '强匹配'
    if score >= 75:
        return '较强匹配'
    if score >= 65:
        return '可重点观察'
    return '普通候选'


# ==================== 数据层 ====================
# ==================== 数据源适配 ====================
def source_candidates(preferred=None, intraday=False):
    pref = (preferred or DATA_SOURCE_MODE or 'auto').lower()
    if pref == 'auto':
        # 日线自动模式：BaoStock → 新浪 → Tushare → AkShare；BaoStock作为主数据源，新浪只在失败时兜底。
        # 免费/无需 Token 的新浪作为第二个日线兜底；Tushare 有 Token 才启用。
        if not intraday:
            # 自动日线：优先BaoStock，失败再回退到新浪；保证默认使用字段更完整的主数据源。
            candidates = ['baostock', 'sina']
            if TUSHARE_TOKEN and ts is not None:
                candidates.append('tushare')
            if ak is not None:
                candidates.append('akshare')
        else:
            # 盘中全市场优先走新浪批量行情，避免3172只股票逐只请求5分钟接口导致全空/超慢；
            # 精细分钟线仍可在自选股/重点股票场景下使用。
            candidates = ['sina_realtime', 'baostock']
            if TUSHARE_TOKEN and ts is not None:
                candidates.append('tushare')
            if ak is not None:
                candidates.append('akshare')
        return candidates
    return [pref]


def get_tushare_pro():
    if ts is None:
        raise RuntimeError('未安装 tushare，请执行：pip install tushare')
    if not TUSHARE_TOKEN:
        raise RuntimeError('未配置 TUSHARE_TOKEN')
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def _tushare_code(code):
    code = normalize_code(code)
    return f'{code}.SH' if code.startswith('6') else f'{code}.SZ'


def fetch_daily_from_tushare(code, start_date, end_date):
    pro = get_tushare_pro()
    ts_code = _tushare_code(code)
    df = pro.daily(ts_code=ts_code, start_date=start_date.replace('-',''), end_date=end_date.replace('-',''))
    if df is None or df.empty:
        raise RuntimeError('Tushare日线返回0条')
    df = df.sort_values('trade_date')
    raw = pd.DataFrame({
        '日期': pd.to_datetime(df['trade_date']),
        '开盘': df['open'], '最高': df['high'], '最低': df['low'], '收盘': df['close'],
        '成交量': df['vol'], '成交额': df['amount'] * 1000.0, '涨跌幅': df['pct_chg']
    })
    # 日线复权由 adj_factor 计算：用当期价格 * 最新复权因子/历史因子形成前复权近似。
    fac = pro.adj_factor(ts_code=ts_code, start_date=start_date.replace('-',''), end_date=end_date.replace('-',''))
    if fac is not None and not fac.empty:
        fac = fac.sort_values('trade_date').copy()
        fac['日期'] = pd.to_datetime(fac['trade_date'])
        raw = raw.merge(fac[['日期','adj_factor']], on='日期', how='left')
    if 'adj_factor' in raw.columns and raw['adj_factor'].notna().any():
        factor = raw['adj_factor'].astype(float)
        latest = factor.iloc[-1]
        qfq = raw.copy()
        scale = factor / latest if latest else 1.0
        for col in ['开盘','最高','最低','收盘']:
            qfq[col] = qfq[col].astype(float) * scale
        return qfq, raw.drop(columns=['adj_factor'], errors='ignore')
    return raw.copy(), raw



def _sina_realtime_symbol(code):
    code = normalize_code(code)
    return f'sh{code}' if code.startswith('6') else f'sz{code}'


def fetch_intraday_realtime_sina_bulk(codes):
    """新浪批量最新行情：适合全市场/大批量盘中采集。
    返回 {code: one-row DataFrame}，不依赖5分钟历史接口。"""
    codes = list(dict.fromkeys(normalize_code(c) for c in codes))
    result = {}
    errors = []
    # URL过长时分块请求，150只左右较稳妥。
    for start in range(0, len(codes), 150):
        chunk = codes[start:start+150]
        symbols = ','.join(_sina_realtime_symbol(c) for c in chunk)
        url = f'https://hq.sinajs.cn/list={symbols}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn/'
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode('gbk', errors='ignore')
            for line in text.splitlines():
                if '="' not in line:
                    continue
                left, right = line.split('="', 1)
                payload = right.rstrip('";')
                code_match = left.split('hq_str_')[-1].strip()
                if not payload:
                    continue
                parts = payload.split(',')
                if len(parts) < 32:
                    continue
                try:
                    code = normalize_code(code_match[-6:])
                    name = parts[0]
                    now_time_text = parts[30] if len(parts) > 30 else ''
                    # 新浪实时接口：今开、昨收、现价、最高、最低、成交量、成交额
                    day_open = safe_float(parts[1])
                    prev_close = safe_float(parts[2])
                    last = safe_float(parts[3])
                    high = safe_float(parts[4])
                    low = safe_float(parts[5])
                    volume = safe_float(parts[8])
                    amount = safe_float(parts[9])
                    if last is None:
                        continue
                    pct = ((last / prev_close) - 1) * 100 if prev_close else None
                    # 伪装成单根“当前快照Bar”，后续统一走快照存储；分钟动量由更精细查询补充。
                    row = pd.DataFrame([{
                        '日期': today_str(),
                        '时间': now_time_text or dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        '代码': code,
                        '开盘': day_open,
                        '最高': high,
                        '最低': low,
                        '收盘': last,
                        '成交量': volume,
                        '成交额': amount,
                        '复权': '3'
                    }])
                    row.attrs['name'] = name
                    row.attrs['prev_close'] = prev_close
                    row.attrs['pct_chg'] = pct
                    row.attrs['source'] = 'sina_realtime'
                    result[code] = row
                except Exception as exc:
                    errors.append(f'{code_match}: 新浪解析失败：{exc}')
        except Exception as exc:
            errors.append(f'新浪批量请求 {chunk[0]}~{chunk[-1]} 失败：{exc}')
    return result, errors


def fetch_intraday_from_sina(code, trade_date):
    """新浪5分钟历史K线。用于重点股/查询，不建议对全市场逐只调用。"""
    symbol = _sina_symbol(code)
    params = urllib.parse.urlencode({'symbol': symbol, 'scale': '5', 'ma': 'no', 'datalen': '1023'})
    url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode('utf-8', errors='ignore')
    if not text or text.strip() in ('null', '[]'):
        raise RuntimeError('新浪5分钟返回0条')
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        raise RuntimeError('新浪5分钟格式异常')
    df = pd.DataFrame(data)
    if 'day' not in df.columns:
        raise RuntimeError('新浪5分钟缺少day字段')
    df['day'] = df['day'].astype(str)
    df = df[df['day'].str.startswith(trade_date)]
    if df.empty:
        raise RuntimeError(f'新浪5分钟在{trade_date}没有数据')
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    if 'amount' in df.columns:
        df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
    else:
        df['amount'] = np.nan
    return pd.DataFrame({
        '日期': trade_date,
        '时间': df['day'].str[-8:],
        '代码': normalize_code(code),
        '开盘': df['open'], '最高': df['high'], '最低': df['low'], '收盘': df['close'],
        '成交量': df['volume'], '成交额': df['amount'], '复权': '3'
    }).dropna(subset=['收盘']).sort_values('时间').reset_index(drop=True)


def fetch_intraday_from_tushare(code, trade_date):
    pro = get_tushare_pro()
    ts_code = _tushare_code(code)
    df = pro.rt_min(ts_code=ts_code, freq='5MIN')
    if df is None or df.empty:
        raise RuntimeError('Tushare实时分钟返回0条')
    df = df.copy()
    time_col = 'time' if 'time' in df.columns else ('trade_time' if 'trade_time' in df.columns else None)
    if not time_col:
        raise RuntimeError('Tushare分钟数据缺少时间字段')
    df[time_col] = df[time_col].astype(str)
    df = df[df[time_col].str.startswith(trade_date)]
    if df.empty:
        raise RuntimeError('Tushare当日5分钟返回0条')
    out = pd.DataFrame({
        '日期': trade_date, '时间': df[time_col], '代码': code,
        '开盘': df['open'], '最高': df['high'], '最低': df['low'], '收盘': df['close'],
        '成交量': df['vol'], '成交额': df['amount'], '复权': '3'
    })
    return out.sort_values('时间').reset_index(drop=True)


def fetch_intraday_from_akshare(code, trade_date):
    if ak is None:
        raise RuntimeError('未安装 akshare，请执行：pip install akshare')
    # AkShare 常见分钟接口可能随上游调整；作为可选备用源，失败时明确回退。
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=normalize_code(code), start_date=f'{trade_date} 09:25:00', end_date=f'{trade_date} 15:05:00', period='5', adjust='')
    except Exception as exc:
        raise RuntimeError(f'AkShare分钟接口失败：{exc}')
    if df is None or df.empty:
        raise RuntimeError('AkShare分钟返回0条')
    df = df.rename(columns={
        '时间':'时间','开盘':'开盘','收盘':'收盘','最高':'最高','最低':'最低','成交量':'成交量','成交额':'成交额'
    })
    if '时间' not in df.columns:
        raise RuntimeError('AkShare分钟数据缺少时间字段')
    out = pd.DataFrame({
        '日期': trade_date, '时间': df['时间'].astype(str), '代码': normalize_code(code),
        '开盘': pd.to_numeric(df['开盘'], errors='coerce'), '最高': pd.to_numeric(df['最高'], errors='coerce'),
        '最低': pd.to_numeric(df['最低'], errors='coerce'), '收盘': pd.to_numeric(df['收盘'], errors='coerce'),
        '成交量': pd.to_numeric(df['成交量'], errors='coerce'), '成交额': pd.to_numeric(df['成交额'], errors='coerce'), '复权':'3'
    })
    return out.dropna(subset=['收盘']).sort_values('时间').reset_index(drop=True)


def _sina_symbol(code):
    code = normalize_code(code)
    return f'sh{code}' if code.startswith('6') else f'sz{code}'


def fetch_daily_from_sina(code, start_date, end_date):
    """新浪历史日线兜底源。
    说明：公开接口历史结构相对简单，本适配器优先保证“日K可用性”。
    新浪公开 K 线接口主要返回一套历史价格序列，因此 qfq/raw 在兜底模式下采用同一序列；
    使用于观察/筛选可以，但严肃回测仍建议优先使用可提供明确复权字段的数据源。
    """
    symbol = _sina_symbol(code)
    params = urllib.parse.urlencode({
        'symbol': symbol,
        'scale': '240',
        'ma': 'no',
        'datalen': '1023',
    })
    url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=12) as resp:
        text = resp.read().decode('utf-8', errors='ignore')
    if not text or text.strip() in ('null', '[]'):
        raise RuntimeError('新浪日线返回0条')
    import json as _json
    data = _json.loads(text)
    if not isinstance(data, list) or not data:
        raise RuntimeError('新浪日线数据格式异常')
    df = pd.DataFrame(data)
    required = {'day', 'open', 'high', 'low', 'close', 'volume'}
    if not required.issubset(df.columns):
        raise RuntimeError(f'新浪日线缺少字段：{sorted(required - set(df.columns))}')
    df['日期'] = pd.to_datetime(df['day'], errors='coerce')
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    if 'amount' in df.columns:
        amount = pd.to_numeric(df['amount'], errors='coerce')
    else:
        amount = pd.Series(np.nan, index=df.index)
    df['成交额'] = amount
    df = df.dropna(subset=['日期','close']).sort_values('日期')
    df = df[(df['日期'] >= pd.Timestamp(start_date)) & (df['日期'] <= pd.Timestamp(end_date))]
    if df.empty:
        raise RuntimeError('新浪在目标日期范围内无数据')
    out = pd.DataFrame({
        '日期': df['日期'], '开盘': df['open'], '最高': df['high'], '最低': df['low'],
        '收盘': df['close'], '成交量': df['volume'], '成交额': df['成交额'],
    })
    out['涨跌幅'] = out['收盘'].pct_change() * 100
    out['涨跌幅'] = out['涨跌幅'].fillna(0.0)
    out = out.reset_index(drop=True)
    return out.copy(), out.copy()



# ==================== BaoStock连接自愈 ====================
def _is_bs_network_error(exc):
    """判断异常是否更像BaoStock网络/Socket异常，而非股票本身无数据。"""
    s = str(exc or '').lower()
    keywords = [
        '10002007', '网络接收错误', 'winerror 10038',
        'invalid distance too far back', 'decompress',
        'utf-8', 'connection reset', 'connection aborted',
        'connection broken', 'broken pipe', 'timed out',
        'socket', 'recv', 'eof'
    ]
    return any(k.lower() in s for k in keywords)


def _bs_login():
    """建立一个全新的BaoStock连接。"""
    try:
        try:
            bs.logout()
        except Exception:
            pass
        time.sleep(BS_RECONNECT_WAIT)
    except Exception:
        pass
    last_msg = ''
    for attempt in range(1, 4):
        try:
            lg = bs.login()
            if getattr(lg, 'error_code', None) == '0':
                logger.info(f'BaoStock重新连接成功（第{attempt}次）')
                return True
            last_msg = f'{getattr(lg, "error_code", "")}:{getattr(lg, "error_msg", "")}'
        except Exception as exc:
            last_msg = str(exc)
        time.sleep(min(BS_BACKOFF_MAX, BS_RECONNECT_WAIT * attempt))
    logger.error(f'BaoStock重新连接失败：{last_msg}')
    return False


def _bs_safe_logout():
    try:
        bs.logout()
    except Exception:
        pass

class DataManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stock_list = None
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS stock_list (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    trade_status TEXT,
                    last_update_date TEXT
                )
            ''')
            cols = {row[1] for row in conn.execute('PRAGMA table_info(stock_list)').fetchall()}
            if 'trade_status' not in cols:
                conn.execute('ALTER TABLE stock_list ADD COLUMN trade_status TEXT')
            if 'last_update_date' not in cols:
                conn.execute('ALTER TABLE stock_list ADD COLUMN last_update_date TEXT')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_data (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    pct_chg REAL,
                    adjust_flag TEXT NOT NULL,
                    PRIMARY KEY (code, trade_date, adjust_flag)
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS intraday_bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    frequency TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS intraday_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetched_at TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    code TEXT NOT NULL,
                    last_price REAL,
                    day_open REAL,
                    day_high REAL,
                    day_low REAL,
                    volume REAL,
                    amount REAL,
                    pct_chg REAL,
                    latest_bar_time TEXT,
                    source TEXT NOT NULL DEFAULT 'baostock_5m'
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS intraday_fetch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetched_at TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    source TEXT,
                    status TEXT NOT NULL,
                    latest_bar_time TEXT,
                    error_message TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS screen_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    screen_time TEXT NOT NULL,
                    screen_date TEXT,
                    data_mode TEXT,
                    params TEXT,
                    result_codes TEXT,
                    note TEXT
                )
            ''')

            # V1.1兼容
            s_cols = {row[1] for row in conn.execute('PRAGMA table_info(screen_records)').fetchall()}
            if 'screen_date' not in s_cols:
                conn.execute('ALTER TABLE screen_records ADD COLUMN screen_date TEXT')
            if 'data_mode' not in s_cols:
                conn.execute('ALTER TABLE screen_records ADD COLUMN data_mode TEXT')

            conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily_data(code, trade_date)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_data(trade_date)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_intraday_code_date ON intraday_bars(code, trade_date)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_intraday_snapshot ON intraday_snapshots(code, trade_date, snapshot_time)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_intraday_log ON intraday_fetch_log(trade_date, fetched_at, code)')
            conn.commit()
            # V1.14.5+ 断点续传任务表兼容：旧数据库可能尚未创建这两张表，先确保表存在，再创建索引。
            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_update_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    total_codes INTEGER NOT NULL DEFAULT 0,
                    completed_codes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    source_mode TEXT,
                    started_at TEXT,
                    updated_at TEXT,
                    finished_at TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_update_items (
                    job_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    used_source TEXT,
                    message TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (job_id, code),
                    FOREIGN KEY (job_id) REFERENCES daily_update_jobs(job_id)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS shareholder_data (
                    code TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    announce_date TEXT,
                    holder_count REAL,
                    prev_holder_count REAL,
                    holder_change REAL,
                    holder_change_pct REAL,
                    avg_hold_value REAL,
                    avg_hold_quantity REAL,
                    total_market_value REAL,
                    total_share_capital REAL,
                    source TEXT,
                    PRIMARY KEY (code, report_date, announce_date)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS stock_profile_cache (
                    snapshot_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    industry TEXT,
                    theme TEXT,
                    total_market_cap REAL,
                    circulating_market_cap REAL,
                    source TEXT,
                    fetched_at TEXT,
                    PRIMARY KEY (snapshot_date, code)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_profile_code_date ON stock_profile_cache(code, snapshot_date)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_shareholder_code_date ON shareholder_data(code, report_date)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_shareholder_announce ON shareholder_data(announce_date, report_date)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_update_jobs ON daily_update_jobs(target_date, status, job_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_update_items ON daily_update_items(job_id, code, status)')
            conn.commit()

    # ---------- 筹码 / 股东户数 ----------
    def get_shareholder_map(self, target_date):
        target_date = str(target_date)
        out = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                if not table_exists_sqlite(conn, 'shareholder_data'):
                    return out
                df = pd.read_sql_query('''
                    SELECT code, report_date, announce_date, holder_count, prev_holder_count,
                           holder_change, holder_change_pct, avg_hold_value, avg_hold_quantity
                    FROM shareholder_data
                    WHERE (announce_date IS NULL OR announce_date <= ?)
                      AND report_date <= ?
                    ORDER BY code, report_date DESC, announce_date DESC
                ''', conn, params=(target_date, target_date))
            if df.empty:
                return out
            df['code'] = df['code'].map(normalize_code)
            for code, g in df.groupby('code', sort=False):
                out[code] = g.iloc[0].to_dict()
        except Exception as exc:
            logger.warning(f'读取股东户数失败：{exc}')
        return out

    def get_recent_quarter_ends(self, count=6):
        """返回最近若干个已结束季度的季度末日期（YYYYMMDD）。
        股东户数接口要求使用季度末日期，例如 20250331、20250630；
        20250401 这类日期不是合法季度末，容易导致整批返回失败。
        """
        today = dt.date.today()
        # 最近一个已经结束的季度
        if today.month <= 3:
            last = dt.date(today.year - 1, 12, 31)
        elif today.month <= 6:
            last = dt.date(today.year, 3, 31)
        elif today.month <= 9:
            last = dt.date(today.year, 6, 30)
        else:
            last = dt.date(today.year, 9, 30)

        ends = []
        cur = last
        for _ in range(max(1, int(count))):
            ends.append(cur.strftime('%Y%m%d'))
            if cur.month == 3:
                cur = dt.date(cur.year - 1, 12, 31)
            elif cur.month == 6:
                cur = dt.date(cur.year, 3, 31)
            elif cur.month == 9:
                cur = dt.date(cur.year, 6, 30)
            else:
                cur = dt.date(cur.year, 9, 30)
        return ends

    def shareholder_quarter_exists(self, quarter):
        """检查某季度是否已经有股东户数数据。"""
        try:
            qdate = pd.to_datetime(str(quarter), format='%Y%m%d', errors='coerce')
            report_date = qdate.strftime('%Y-%m-%d') if pd.notna(qdate) else str(quarter)
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM shareholder_data WHERE report_date=?",
                    (report_date,)
                ).fetchone()
            return bool(row and row[0] > 0)
        except Exception:
            return False

    def update_shareholder_if_needed(self, force=False):
        """收盘更新后的轻量股东数据同步。
        只检查最近两个已结束季度；已存在则跳过，缺失才抓取。
        """
        if ak is None:
            logger.info('AKShare未安装，跳过股东户数同步。')
            return {'enabled': False, 'saved': 0, 'skipped': 0, 'errors': ['AKShare未安装']}

        quarters = self.get_recent_quarter_ends(2)
        targets = [q for q in quarters if force or not self.shareholder_quarter_exists(q)]
        if not targets:
            logger.info(f'股东户数无需更新：最近季度 {",".join(quarters)} 均已有缓存。')
            return {'enabled': True, 'saved': 0, 'skipped': len(quarters), 'errors': []}

        try:
            result = self.update_shareholder_history(quarters=targets)
            result['skipped'] = len(quarters) - len(targets)
            return result
        except Exception as exc:
            logger.warning(f'股东户数同步失败（不影响行情筛选）：{exc}')
            return {'enabled': True, 'saved': 0, 'skipped': 0, 'errors': [str(exc)]}

    def update_shareholder_history(self, quarters=None, progress_callback=None):
        if ak is None:
            raise RuntimeError('未安装 akshare，请先执行：pip install akshare')
        quarters = quarters or self.get_recent_quarter_ends(6)
        total_saved = 0
        errors = []
        for idx, quarter in enumerate(quarters, 1):
            try:
                logger.info(f'开始获取股东户数：{quarter}')
                df = ak.stock_zh_a_gdhs(symbol=quarter)
                if df is None or df.empty:
                    raise RuntimeError(f'接口返回空数据（季度参数={quarter}）')
                logger.info(f'股东户数接口返回：{quarter}，{len(df)}行；字段={list(df.columns)}')
                cols = {str(c).strip(): c for c in df.columns}
                def pick(*names):
                    for name in names:
                        if name in cols:
                            return cols[name]
                    return None
                code_col = pick('代码')
                rep_col = pick('股东户数统计截止日-本次', '股东户数统计截止日')
                ann_col = pick('公告日期', '股东户数公告日期')
                holder_col = pick('股东户数-本次')
                prev_col = pick('股东户数-上次')
                change_col = pick('股东户数-增减')
                pct_col = pick('股东户数-增减比例')
                avgv_col = pick('户均持股市值')
                avgq_col = pick('户均持股数量')
                mv_col = pick('总市值')
                cap_col = pick('总股本')
                if not code_col or not holder_col:
                    raise RuntimeError(f'缺少代码/股东户数字段：{list(df.columns)}')
                rows = []
                for _, r in df.iterrows():
                    code = normalize_code(r.get(code_col))
                    if not code.startswith(STOCK_PREFIXES):
                        continue
                    rep = pd.to_datetime(r.get(rep_col), errors='coerce') if rep_col else pd.Timestamp(quarter)
                    ann = pd.to_datetime(r.get(ann_col), errors='coerce') if ann_col else pd.NaT
                    def num(col):
                        return safe_float(r.get(col)) if col else None
                    rows.append((
                        code,
                        rep.strftime('%Y-%m-%d') if pd.notna(rep) else quarter,
                        ann.strftime('%Y-%m-%d') if pd.notna(ann) else None,
                        num(holder_col), num(prev_col), num(change_col), num(pct_col),
                        num(avgv_col), num(avgq_col), num(mv_col), num(cap_col),
                        'AKShare-东方财富'
                    ))
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany('''
                        INSERT INTO shareholder_data
                        (code, report_date, announce_date, holder_count, prev_holder_count,
                         holder_change, holder_change_pct, avg_hold_value, avg_hold_quantity,
                         total_market_value, total_share_capital, source)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(code, report_date, announce_date) DO UPDATE SET
                            holder_count=excluded.holder_count,
                            prev_holder_count=excluded.prev_holder_count,
                            holder_change=excluded.holder_change,
                            holder_change_pct=excluded.holder_change_pct,
                            avg_hold_value=excluded.avg_hold_value,
                            avg_hold_quantity=excluded.avg_hold_quantity,
                            total_market_value=excluded.total_market_value,
                            total_share_capital=excluded.total_share_capital,
                            source=excluded.source
                    ''', rows)
                    conn.commit()
                total_saved += len(rows)
                logger.info(f'股东户数完成：{quarter}，{len(rows)}只')
            except Exception as exc:
                errors.append((quarter, str(exc)))
                logger.warning(f'股东户数失败：{quarter}：{exc}')
            if progress_callback:
                progress_callback(idx, len(quarters), quarter)
        return {'quarters': len(quarters), 'saved': total_saved, 'errors': errors}

    # ---------- 股票列表 ----------
    def get_stock_list(self, force_refresh=False):
        if self.stock_list is not None and not self.stock_list.empty and not force_refresh:
            return self.stock_list
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query(
                    'SELECT code, name, trade_status, last_update_date FROM stock_list', conn
                )
            if not df.empty:
                df['code'] = df['code'].map(normalize_code)
                if '000917' in set(df['code']):
                    df.loc[df['code'] == '000917', 'name'] = KNOWN_STOCK_NAME_FIXES['000917']
                    try:
                        with sqlite3.connect(self.db_path) as cfix:
                            cfix.execute('UPDATE stock_list SET name=? WHERE code=?',(KNOWN_STOCK_NAME_FIXES['000917'],'000917'))
                            cfix.commit()
                    except Exception:
                        pass
                self.stock_list = df
                return df
        except Exception as exc:
            print(f'读取股票列表失败：{exc}')

        df = self._fetch_stock_list_from_baostock()
        self._save_stock_list(df)
        self.stock_list = df
        return df

    def _fetch_stock_list_from_baostock(self):
        # 主数据优先使用 query_stock_basic，减少指数/非个股证券混入股票主表的风险。
        data_list=[]
        try:
            lg=bs.login()
            if lg.error_code=='0':
                rs=bs.query_stock_basic()
                while rs.error_code=='0' and rs.next(): data_list.append(rs.get_row_data())
            try: bs.logout()
            except Exception: pass
        except Exception as exc:
            print(f'query_stock_basic失败：{exc}')
            try: bs.logout()
            except Exception: pass
        if not data_list:
            for days_back in range(5):
                query_day=(dt.datetime.now()-dt.timedelta(days=days_back)).strftime('%Y-%m-%d')
                try:
                    lg=bs.login()
                    if lg.error_code!='0': continue
                    rs=bs.query_all_stock(day=query_day)
                    while rs.error_code=='0' and rs.next(): data_list.append(rs.get_row_data())
                    bs.logout()
                    if data_list: break
                except Exception as exc:
                    print(f'获取股票列表失败 {query_day}: {exc}')
                    try: bs.logout()
                    except Exception: pass
        if not data_list: raise RuntimeError('获取股票列表失败，请检查网络或稍后再试。')
        df=pd.DataFrame(data_list)
        if df.shape[1]<3: raise RuntimeError('股票列表数据格式异常。')
        df=df.iloc[:,:3]; df.columns=['code','tradeStatus','code_name']
        df['code']=df['code'].map(normalize_code)
        df=df[df['code'].str.startswith(STOCK_PREFIXES)]
        df=df[~df['code_name'].str.contains('ST|退',na=False)]
        df=df[df['tradeStatus'].astype(str).isin(['1','上市'])]
        for _code,_name in KNOWN_STOCK_NAME_FIXES.items(): df.loc[df['code']==_code,'code_name']=_name
        df=df[['code','code_name']].rename(columns={'code_name':'name'})
        df['trade_status']='1'; df['last_update_date']=None
        return df.drop_duplicates('code').reset_index(drop=True)

    def _save_stock_list(self, df):
        rows = []
        for _, row in df.iterrows():
            rows.append((
                normalize_code(row['code']), str(row['name']),
                str(row.get('trade_status', '1')),
                row.get('last_update_date') if pd.notna(row.get('last_update_date')) else None
            ))
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany('''
                INSERT INTO stock_list(code, name, trade_status, last_update_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,
                    trade_status=excluded.trade_status
            ''', rows)
            conn.commit()

    def repair_stock_master_names(self):
        """从 BaoStock 基本信息重新校验名称，只修改 stock_list，不碰行情数据。"""
        fresh=self._fetch_stock_list_from_baostock(); old=self.get_stock_list().copy()
        merged=old.merge(fresh[['code','name']],on='code',how='left',suffixes=('_old','_new')); changes=[]
        with sqlite3.connect(self.db_path) as conn:
            for _,r in merged.iterrows():
                code=normalize_code(r['code']); old_name=str(r.get('name_old','')); new_name=str(r.get('name_new','')) if pd.notna(r.get('name_new')) else old_name
                if code in KNOWN_STOCK_NAME_FIXES: new_name=KNOWN_STOCK_NAME_FIXES[code]
                if new_name and new_name!=old_name:
                    conn.execute('UPDATE stock_list SET name=? WHERE code=?',(new_name,code)); changes.append((code,old_name,new_name))
            conn.commit()
        self.stock_list=None; self.get_stock_list(); return changes

    def inspect_date(self,target_date):
        target_date=parse_date(target_date).strftime('%Y-%m-%d')
        with sqlite3.connect(self.db_path) as conn:
            q=conn.execute("SELECT COUNT(DISTINCT code) FROM daily_data WHERE trade_date=? AND adjust_flag='qfq'",(target_date,)).fetchone()[0]
            r=conn.execute("SELECT COUNT(DISTINCT code) FROM daily_data WHERE trade_date=? AND adjust_flag='raw'",(target_date,)).fetchone()[0]
            prev=conn.execute("SELECT MAX(trade_date) FROM daily_data WHERE trade_date<? AND adjust_flag='raw'",(target_date,)).fetchone()[0]
            nxt=conn.execute("SELECT MIN(trade_date) FROM daily_data WHERE trade_date>? AND adjust_flag='raw'",(target_date,)).fetchone()[0]
        return {'date':target_date,'qfq_count':int(q or 0),'raw_count':int(r or 0),'prev_date':prev,'next_date':nxt}

    def compare_date_data(self,other_db_path,target_date,tolerance=1e-6):
        target_date=parse_date(target_date).strftime('%Y-%m-%d')
        with sqlite3.connect(self.db_path) as c1,sqlite3.connect(other_db_path) as c2:
            a=pd.read_sql_query("SELECT code,open,high,low,close,volume,amount,pct_chg FROM daily_data WHERE trade_date=? AND adjust_flag='raw'",c1,params=(target_date,))
            b=pd.read_sql_query("SELECT code,open,high,low,close,volume,amount,pct_chg FROM daily_data WHERE trade_date=? AND adjust_flag='raw'",c2,params=(target_date,))
        for df in (a,b):
            if not df.empty: df['code']=df['code'].map(normalize_code)
        common=sorted(set(a['code'])&set(b['code'])) if not a.empty and not b.empty else []
        diffs=[]
        if common:
            aa=a[a.code.isin(common)].set_index('code'); bb=b[b.code.isin(common)].set_index('code')
            for code in common:
                bad=[]
                for f in ['open','high','low','close','volume','amount','pct_chg']:
                    va,vb=aa.loc[code,f],bb.loc[code,f]
                    if pd.isna(va) and pd.isna(vb): continue
                    if pd.isna(va)!=pd.isna(vb) or abs(float(va)-float(vb))>tolerance*max(1.0,abs(float(va)) if pd.notna(va) else 0.0,abs(float(vb)) if pd.notna(vb) else 0.0): bad.append(f)
                if bad: diffs.append({'代码':code,'差异字段':'、'.join(bad)})
        return {'target_date':target_date,'main_count':len(a),'other_count':len(b),'common':len(common),'mismatch':len(diffs),'details':pd.DataFrame(diffs)}

    def get_stock_name(self, code):
        code = normalize_code(code)
        df = self.get_stock_list()
        row = df[df['code'] == code]
        return str(row.iloc[0]['name']) if not row.empty else ''

    def get_batches(self, count=SECTOR_COUNT):
        if self.stock_list is None or self.stock_list.empty:
            self.get_stock_list()
        codes = self.stock_list['code'].tolist()
        total = len(codes)
        base, remainder = divmod(total, count)
        batches, start = [], 0
        for i in range(count):
            size = base + (1 if i < remainder else 0)
            batches.append(codes[start:start + size])
            start += size
        return batches

    # ---------- 日线 ----------
    def fetch_daily_single(self, code, start_date, end_date, source=None):
        """单只股票日线获取：BaoStock网络异常时自动重建连接；源仍按auto顺序兜底。"""
        code = normalize_code(code)
        errors = []
        for src in source_candidates(source):
            for attempt in range(MAX_RETRIES + 1):
                try:
                    if src == 'baostock':
                        bs_code = format_baostock_code(code)
                        fields = 'date,open,high,low,close,volume,amount,pctChg'
                        q = bs.query_history_k_data_plus(
                            bs_code, fields, start_date=start_date, end_date=end_date,
                            frequency='d', adjustflag='2'
                        )
                        r = bs.query_history_k_data_plus(
                            bs_code, fields, start_date=start_date, end_date=end_date,
                            frequency='d', adjustflag='3'
                        )
                        if q.error_code != '0' or r.error_code != '0':
                            raise RuntimeError(
                                f'BaoStock错误 q={q.error_code}:{getattr(q,"error_msg","")}'
                                f' r={r.error_code}:{getattr(r,"error_msg","")}'
                            )

                        q_rows, r_rows = [], []
                        while q.error_code == '0' and q.next():
                            q_rows.append(q.get_row_data())
                        while r.error_code == '0' and r.next():
                            r_rows.append(r.get_row_data())

                        if not q_rows or not r_rows:
                            raise RuntimeError('BaoStock日线返回0条')

                        cols = ['日期','开盘','最高','最低','收盘','成交量','成交额','涨跌幅']
                        qdf = pd.DataFrame(q_rows, columns=cols)
                        rdf = pd.DataFrame(r_rows, columns=cols)
                        numeric = ['开盘','最高','最低','收盘','成交量','成交额','涨跌幅']
                        for c in numeric:
                            qdf[c] = pd.to_numeric(qdf[c], errors='coerce')
                            rdf[c] = pd.to_numeric(rdf[c], errors='coerce')
                        qdf['日期'] = pd.to_datetime(qdf['日期'], errors='coerce')
                        rdf['日期'] = pd.to_datetime(rdf['日期'], errors='coerce')
                        qdf = qdf.dropna(subset=['日期','收盘']).sort_values('日期').reset_index(drop=True)
                        rdf = rdf.dropna(subset=['日期','收盘']).sort_values('日期').reset_index(drop=True)
                        if qdf.empty or rdf.empty:
                            raise RuntimeError('BaoStock返回数据但有效收盘价为空')
                        return qdf, rdf, src

                    if src == 'sina':
                        qdf, rdf = fetch_daily_from_sina(code, start_date, end_date)
                        return qdf, rdf, src

                    if src == 'tushare':
                        qdf, rdf = fetch_daily_from_tushare(code, start_date, end_date)
                        return qdf, rdf, src

                    if src == 'akshare':
                        raise RuntimeError('AkShare日线备用源暂不用于核心历史复权数据')

                except Exception as exc:
                    msg = f'{src}: {exc}'
                    errors.append(msg)

                    # BaoStock网络类错误：先重建连接再重试，而不是继续使用坏socket。
                    if src == 'baostock' and _is_bs_network_error(exc):
                        if attempt < MAX_RETRIES:
                            wait_s = min(BS_BACKOFF_MAX, BS_RECONNECT_WAIT * (attempt + 1))
                            logger.warning(
                                f'{code}: BaoStock网络异常，第{attempt+1}次重试前重建连接，等待{wait_s:.1f}s：{exc}'
                            )
                            _bs_safe_logout()
                            time.sleep(wait_s)
                            if not _bs_login():
                                logger.warning(f'{code}: BaoStock重连失败，本次将继续尝试下一数据源。')
                                break
                            continue

                    if attempt < MAX_RETRIES:
                        wait_s = min(BS_BACKOFF_MAX, 0.8 * (attempt + 1))
                        time.sleep(wait_s)
                    else:
                        break

        raise RuntimeError('；'.join(errors[-8:]) or '所有数据源均未返回数据')

    def save_daily_data(self, code, qfq_df, raw_df):
        rows = []
        code = normalize_code(code)
        for flag, df in [('qfq', qfq_df), ('raw', raw_df)]:
            if df is None:
                continue
            for row in df.itertuples(index=False):
                rows.append((code, row.日期.strftime('%Y-%m-%d'),
                             safe_float(row.开盘), safe_float(row.最高), safe_float(row.最低),
                             safe_float(row.收盘), safe_float(row.成交量), safe_float(row.成交额),
                             safe_float(row.涨跌幅), flag))
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany('''
                INSERT OR REPLACE INTO daily_data
                (code, trade_date, open, high, low, close, volume, amount, pct_chg, adjust_flag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', rows)
            conn.commit()

    def save_daily_data_batch(self, items):
        """批量写入日线，减少SQLite连接和commit次数。"""
        rows = []
        latest_dates = []
        for code, qfq_df, raw_df in items:
            code = normalize_code(code)
            for flag, frame in [('qfq', qfq_df), ('raw', raw_df)]:
                if frame is None or frame.empty:
                    continue
                for row in frame.itertuples(index=False):
                    rows.append((code, row.日期.strftime('%Y-%m-%d'), safe_float(row.开盘), safe_float(row.最高), safe_float(row.最低),
                                 safe_float(row.收盘), safe_float(row.成交量), safe_float(row.成交额), safe_float(row.涨跌幅), flag))
            if qfq_df is not None and not qfq_df.empty:
                latest_dates.append((qfq_df.iloc[-1]['日期'].strftime('%Y-%m-%d'), code))
        if not rows:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executemany('''
                INSERT OR REPLACE INTO daily_data
                (code, trade_date, open, high, low, close, volume, amount, pct_chg, adjust_flag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', rows)
            if latest_dates:
                conn.executemany('UPDATE stock_list SET last_update_date=? WHERE code=?', latest_dates)
            conn.commit()
        return len(latest_dates)

    def _completed_codes_for_date(self, codes, target_date):
        codes = list(dict.fromkeys(normalize_code(c) for c in codes))
        if not codes:
            return set()
        marks = ','.join('?' for _ in codes)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(f"""
                SELECT code
                FROM daily_data
                WHERE trade_date=? AND code IN ({marks}) AND adjust_flag IN ('qfq','raw')
                GROUP BY code
                HAVING COUNT(DISTINCT adjust_flag)=2
            """, [target_date] + codes).fetchall()
        return {normalize_code(r[0]) for r in rows}

    def get_daily_resume_info(self, codes, target_date):
        # 检测目标收盘日已有多少股票完整落库，可用于断点续传。
        codes = list(dict.fromkeys(normalize_code(c) for c in codes))
        completed = self._completed_codes_for_date(codes, target_date)
        with sqlite3.connect(self.db_path) as conn:
            job = conn.execute("""
                SELECT job_id,status,completed_codes,updated_at
                FROM daily_update_jobs
                WHERE target_date=?
                ORDER BY job_id DESC LIMIT 1
            """, (target_date,)).fetchone()
        return {'total': len(codes), 'completed': len(completed),
                'pending': max(0, len(codes)-len(completed)), 'codes': completed, 'job': job}

    def _create_daily_job(self, target_date, start_date, codes, source_mode):
        now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("""
                INSERT INTO daily_update_jobs
                (target_date,start_date,total_codes,completed_codes,status,source_mode,started_at,updated_at,finished_at)
                VALUES (?,?,?,?,?,?,?,?,NULL)
            """, (target_date, start_date, len(codes), 0, 'running', source_mode or 'auto', now, now))
            conn.commit()
            return cur.lastrowid

    def _mark_daily_items(self, job_id, item_rows):
        if not item_rows:
            return
        now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rows=[(job_id, normalize_code(code), status, used_source or '', message or '', now)
              for code,status,used_source,message in item_rows]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("""
                INSERT INTO daily_update_items(job_id,code,status,used_source,message,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id,code) DO UPDATE SET
                    status=excluded.status, used_source=excluded.used_source,
                    message=excluded.message, updated_at=excluded.updated_at
            """, rows)
            conn.execute("""
                UPDATE daily_update_jobs
                SET completed_codes=(SELECT COUNT(*) FROM daily_update_items
                                     WHERE job_id=? AND status IN ('success','skipped')),
                    updated_at=?
                WHERE job_id=?
            """, (job_id, now, job_id))
            conn.commit()

    def _finish_daily_job(self, job_id, status):
        now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE daily_update_jobs SET status=?,updated_at=?,finished_at=? WHERE job_id=?',
                         (status, now, now, job_id))
            conn.commit()

    def mark_running_daily_jobs_interrupted(self):
        now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE daily_update_jobs SET status='interrupted', updated_at=? WHERE status='running'", (now,))
            conn.commit()

    def update_daily(self, codes, start_date, end_date, overwrite=False, progress_callback=None, source=None):
        # 高性能日线更新 + 断点续传：每20只左右批量落库，重启后只请求目标日期缺失的股票。
        from concurrent.futures import ThreadPoolExecutor, as_completed
        codes = list(dict.fromkeys(normalize_code(c) for c in codes))
        total = len(codes)
        last_map = {} if overwrite else self._get_last_update_dates(codes)
        target_completed = set() if overwrite else self._completed_codes_for_date(codes, end_date)
        starts = {}
        for code in codes:
            if overwrite:
                starts[code] = start_date
            else:
                last_date = last_map.get(code)
                candidate = (parse_date(last_date) + dt.timedelta(days=1)).strftime('%Y-%m-%d') if last_date else start_date
                starts[code] = max(candidate, start_date)
        active = [c for c in codes if starts[c] <= end_date and c not in target_completed]
        skipped = len(codes) - len(active)
        completed = skipped
        success = 0
        failed = 0
        failures = []
        items = []
        item_meta = []
        source_mode = (source or DATA_SOURCE_MODE or 'auto').lower()
        job_id = self._create_daily_job(end_date, start_date, codes, source_mode)
        if target_completed:
            self._mark_daily_items(job_id, [(c,'skipped','database','目标日期已有完整qfq/raw数据') for c in sorted(target_completed)])
        logger.info(f'开始日线更新：总计={total}，有效请求={len(active)}，跳过={skipped}，日期={start_date}~{end_date}，job_id={job_id}')
        if progress_callback:
            progress_callback(completed, total, '断点续传：已完成' if target_completed else '开始')

        def report(code):
            nonlocal completed
            completed += 1
            if progress_callback:
                progress_callback(completed, total, code)

        def flush_batch():
            nonlocal items, item_meta
            if not items:
                return
            written = self.save_daily_data_batch(items)
            self._mark_daily_items(job_id, item_meta)
            logger.info(f'断点批量落库：{written}只，累计数据库完成={completed}/{total}')
            items = []
            item_meta = []

        succeeded = set(target_completed)
        pending = list(active)

        if source_mode in ('auto','baostock') and pending:
            bs_logged_in = _bs_login()
            if bs_logged_in:
                try:
                    next_pending = []
                    for seq, code in enumerate(pending, 1):
                        # 长跑主动换连接，减少socket长期运行后的异常。
                        if seq > 1 and (seq - 1) % BS_RECONNECT_EVERY == 0:
                            logger.info(f'BaoStock主动刷新连接：已处理{seq-1}只，开始新连接。')
                            if not _bs_login():
                                next_pending.extend(pending[seq-1:])
                                break

                        try:
                            qdf, rdf, used = self.fetch_daily_single(
                                code, starts[code], end_date, source='baostock'
                            )
                            if qdf is not None and rdf is not None and not qdf.empty and not rdf.empty:
                                items.append((code, qdf, rdf))
                                item_meta.append((code, 'success', used, ''))
                                succeeded.add(code)
                                success += 1
                                report(code)
                                if len(items) >= BATCH_SAVE_SIZE:
                                    flush_batch()
                            else:
                                next_pending.append(code)
                                failures.append((code, 'BaoStock：返回空数据'))
                        except Exception as exc:
                            msg = f'BaoStock：{exc}'
                            next_pending.append(code)
                            failures.append((code, msg))
                            # 网络异常进入待补队列，后续由新浪兜底；真正无数据则直接记录。
                            if _is_bs_network_error(exc):
                                logger.warning(f'{code}: BaoStock网络异常，已转入兜底队列：{exc}')
                            else:
                                logger.info(f'{code}: BaoStock未返回有效日线：{exc}')
                finally:
                    _bs_safe_logout()

                pending = [c for c in next_pending if c not in succeeded]

        if source_mode == 'auto' and pending:
            workers=min(8,max(4,(os.cpu_count() or 4)))
            def job(code):
                try:
                    qdf,rdf=fetch_daily_from_sina(code,starts[code],end_date)
                    return code,qdf,rdf,'sina',''
                except Exception as exc:
                    return code,None,None,'',str(exc)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures={ex.submit(job,code):code for code in pending}
                for fut in as_completed(futures):
                    code,qdf,rdf,used,err=fut.result()
                    if qdf is not None and rdf is not None and not qdf.empty and not rdf.empty:
                        items.append((code,qdf,rdf)); item_meta.append((code,'success',used,'')); succeeded.add(code); success+=1; report(code)
                        if len(items)>=BATCH_SAVE_SIZE: flush_batch()
                    else:
                        failures.append((code,f'新浪：{err or "返回0条"}'))
            pending=[c for c in pending if c not in succeeded]

        if pending and source_mode not in ('auto','baostock'):
            for code in list(pending):
                try:
                    qdf,rdf,used=self.fetch_daily_single(code,starts[code],end_date,source=source_mode)
                    if qdf is not None and rdf is not None and not qdf.empty and not rdf.empty:
                        items.append((code,qdf,rdf)); item_meta.append((code,'success',used,'')); succeeded.add(code); success+=1; report(code)
                        if len(items)>=BATCH_SAVE_SIZE: flush_batch()
                    else:
                        failed+=1; report(code); msg=f'{source_mode}：返回空数据'; failures.append((code,msg)); self._mark_daily_items(job_id,[(code,'failed',source_mode,msg)])
                except Exception as exc:
                    failed+=1; report(code); msg=f'{source_mode}：{exc}'; failures.append((code,msg)); self._mark_daily_items(job_id,[(code,'failed',source_mode,msg)])

        if source_mode == 'auto' and pending:
            for code in pending:
                failed+=1; report(code)
                reason=next((r for c,r in reversed(failures) if c==code),'所有数据源失败')
                self._mark_daily_items(job_id,[(code,'failed','',reason)])

        flush_batch()
        status='completed' if failed==0 else 'completed_with_errors'
        self._finish_daily_job(job_id,status)
        logger.info(f'日线更新结束：成功={success}，失败={failed}，跳过={skipped}，job_id={job_id}')
        if failures:
            network_failures = sum(1 for _, r in failures if _is_bs_network_error(r))
            logger.info(f'失败分类：网络/连接异常={network_failures}；其他={len(failures)-network_failures}')
        if failures:
            logger.info('--- 日线失败明细（前30只）---')
            for code,reason in failures[:30]: logger.info(f'{code}: {reason}')
        return {'total':total,'success':success,'failed':failed,'skipped':skipped,'failures':failures,
                'job_id':job_id,'resume_completed':len(target_completed)}

    def _get_last_update_dates(self, codes):
        codes = list(dict.fromkeys(normalize_code(c) for c in codes))
        if not codes:
            return {}
        marks = ','.join('?' for _ in codes)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f'SELECT code, last_update_date FROM stock_list WHERE code IN ({marks})', codes
            ).fetchall()
        return {normalize_code(code): value for code, value in rows if value}

    def _get_last_update_date(self, code):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('SELECT last_update_date FROM stock_list WHERE code=?', (normalize_code(code),)).fetchone()
        return row[0] if row and row[0] else None

    def _set_last_update_date(self, code, value):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('UPDATE stock_list SET last_update_date=? WHERE code=?', (value, normalize_code(code)))
            conn.commit()

    def date_status(self, target_date):
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute('SELECT COUNT(*) FROM stock_list').fetchone()[0]
            qfq = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_data WHERE trade_date=? AND adjust_flag='qfq'", (target_date,)).fetchone()[0]
            raw = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_data WHERE trade_date=? AND adjust_flag='raw'", (target_date,)).fetchone()[0]
        return int(total), int(qfq), int(raw)

    # ---------- 盘中5分钟 ----------
    def fetch_intraday_single(self, code, trade_date, source=None):
        code = normalize_code(code)
        errors = []
        for src in source_candidates(source, intraday=True):
            try:
                if src == 'sina_realtime':
                    bulk, bulk_errors = fetch_intraday_realtime_sina_bulk([code])
                    if code not in bulk:
                        raise RuntimeError('新浪实时行情返回0条')
                    return bulk[code], '', src
                if src == 'sina':
                    return fetch_intraday_from_sina(code, trade_date), '', src
                if src == 'baostock':
                    bs_code = format_baostock_code(code)
                    fields = 'date,time,code,open,high,low,close,volume,amount,adjustflag'
                    rs = bs.query_history_k_data_plus(bs_code, fields, start_date=trade_date, end_date=trade_date, frequency=INTRADAY_FREQUENCY, adjustflag='3')
                    if rs.error_code != '0':
                        raise RuntimeError(f'BaoStock错误：{rs.error_code}:{getattr(rs,"error_msg","")}')
                    rows=[]
                    while rs.error_code=='0' and rs.next(): rows.append(rs.get_row_data())
                    if not rows:
                        raise RuntimeError('BaoStock当日5分钟返回0条')
                    cols=['日期','时间','代码','开盘','最高','最低','收盘','成交量','成交额','复权']
                    df=pd.DataFrame(rows,columns=cols)
                    for c in ['开盘','最高','最低','收盘','成交量','成交额']:
                        df[c]=pd.to_numeric(df[c],errors='coerce')
                    df['时间']=df['时间'].astype(str)
                    df=df.dropna(subset=['收盘']).sort_values('时间').reset_index(drop=True)
                    if df.empty: raise RuntimeError('BaoStock返回分钟数据但收盘价为空')
                    return df, '', src
                if src == 'tushare':
                    return fetch_intraday_from_tushare(code, trade_date), '', src
                if src == 'akshare':
                    return fetch_intraday_from_akshare(code, trade_date), '', src
            except Exception as exc:
                errors.append(f'{src}: {exc}')
        return pd.DataFrame(), '；'.join(errors[-6:]) or '未知错误', ''

    def save_intraday_snapshot(self, trade_date, snapshot_time, data_map, default_source='auto'):
        fetched_at = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        bar_rows = []
        snap_rows = []
        for code, payload in data_map.items():
            df = payload.get('df') if isinstance(payload, dict) else payload
            source_name = payload.get('source') if isinstance(payload, dict) else default_source
            if df is None or df.empty:
                continue
            code = normalize_code(code)
            for row in df.itertuples(index=False):
                bar_rows.append((
                    code, trade_date, str(row.时间), safe_float(row.开盘), safe_float(row.最高),
                    safe_float(row.最低), safe_float(row.收盘), safe_float(row.成交量),
                    safe_float(row.成交额), INTRADAY_FREQUENCY, fetched_at
                ))
            latest = df.iloc[-1]
            day_open = safe_float(df.iloc[0]['开盘'])
            day_high = safe_float(df['最高'].max())
            day_low = safe_float(df['最低'].min())
            volume = safe_float(df['成交量'].sum()) or 0
            amount = safe_float(df['成交额'].sum()) or 0
            prev_close = self.get_previous_daily_close(code, trade_date)
            last_price = safe_float(latest['收盘'])
            pct = ((last_price / prev_close) - 1) * 100 if prev_close and last_price is not None else None
            snap_rows.append((
                fetched_at, trade_date, snapshot_time, code, last_price,
                day_open, day_high, day_low, volume, amount, pct, str(latest['时间']), str(source_name or default_source)
            ))
        with sqlite3.connect(self.db_path) as conn:
            if bar_rows:
                conn.executemany('''
                    INSERT INTO intraday_bars
                    (code, trade_date, bar_time, open, high, low, close, volume, amount, frequency, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', bar_rows)
            if snap_rows:
                conn.executemany('''
                    INSERT INTO intraday_snapshots
                    (fetched_at, trade_date, snapshot_time, code, last_price, day_open, day_high,
                     day_low, volume, amount, pct_chg, latest_bar_time, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', snap_rows)
            conn.commit()
        return len(snap_rows)

    def _save_intraday_fetch_log(self, rows):
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("""
                INSERT INTO intraday_fetch_log
                (fetched_at, trade_date, code, source, status, latest_bar_time, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()

    def collect_intraday(self, codes, trade_date=None, progress_callback=None, source=None):
        trade_date = trade_date or today_str()
        if trade_date != today_str():
            raise ValueError('盘中采样只能采今天；历史盘中请使用“查询该日采样”。')
        current = now_time()
        if current < MARKET_OPEN:
            raise ValueError('当前还没到09:30，暂时没有盘中行情。')
        if current >= MARKET_CLOSE:
            raise ValueError('当前已收盘，请使用“收盘复盘”获取正式日线。')
        codes = list(dict.fromkeys(normalize_code(c) for c in codes))
        data_map, errors, log_rows = {}, [], []
        fetched_at = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        candidates = source_candidates(source, intraday=True)
        # 全市场默认优先批量新浪实时行情，避免3172次分钟接口调用。
        if 'sina_realtime' in candidates:
            try:
                bulk, bulk_errors = fetch_intraday_realtime_sina_bulk(codes)
                errors.extend(bulk_errors)
                for idx, code in enumerate(codes, 1):
                    df = bulk.get(code)
                    if df is not None and not df.empty:
                        data_map[code] = {'df': df, 'source': 'sina_realtime'}
                        log_rows.append((fetched_at, trade_date, code, 'sina_realtime', 'success', str(df.iloc[-1]['时间']), ''))
                    else:
                        errors.append(f'{code}：新浪批量实时无数据')
                        log_rows.append((fetched_at, trade_date, code, 'sina_realtime', 'failed', '', '新浪批量实时无数据'))
                    if progress_callback:
                        progress_callback(idx, len(codes), code)
                # 对少量失败代码再走分钟接口/备用源，但全市场成功时不重复打接口。
                fallback_codes = [c for c in codes if c not in data_map]
                if fallback_codes and len(fallback_codes) <= 80:
                    for code in fallback_codes:
                        df, err, used = self.fetch_intraday_single(code, trade_date, source='baostock')
                        if df is not None and not df.empty:
                            data_map[code] = {'df': df, 'source': used}
                        else:
                            errors.append(f'{code}：备用分钟源 {err}')
            except Exception as exc:
                errors.append(f'新浪批量实时总失败：{exc}')
        else:
            # 手动指定非新浪源时，保留原逐只路径。
            bs_logged_in = False
            if 'baostock' in candidates:
                lg = bs.login()
                if lg.error_code == '0':
                    bs_logged_in = True
                else:
                    errors.append(f'BaoStock登录失败：{getattr(lg, "error_msg", "unknown")}')
            try:
                for idx, code in enumerate(codes, 1):
                    df, err, used_source = self.fetch_intraday_single(code, trade_date, source=source)
                    if df is not None and not df.empty:
                        data_map[code] = {'df': df, 'source': used_source}
                        log_rows.append((fetched_at, trade_date, code, used_source, 'success', str(df.iloc[-1]['时间']), ''))
                    else:
                        errors.append(f'{code}：{err}')
                        log_rows.append((fetched_at, trade_date, code, used_source or '', 'failed', '', err or '未知错误'))
                    if progress_callback:
                        progress_callback(idx, len(codes), code)
                    time.sleep(REQUEST_DELAY)
            finally:
                if bs_logged_in:
                    try: bs.logout()
                    except Exception: pass
        snapshot_time = dt.datetime.now().strftime('%H:%M:%S')
        saved = self.save_intraday_snapshot(trade_date, snapshot_time, data_map)
        self._save_intraday_fetch_log(log_rows)
        return {'total': len(codes), 'success': saved, 'snapshot_time': snapshot_time,
                'errors': errors, 'used_sources': sorted(set(v.get('source') for v in data_map.values() if v.get('source')))}

    def query_intraday_snapshots(self, trade_date, snapshot_time=None, codes=None):
        codes = [normalize_code(c) for c in (codes or [])]
        where, params = ['s.trade_date=?'], [trade_date]
        if snapshot_time:
            where.append('s.snapshot_time LIKE ?'); params.append(str(snapshot_time).strip()+'%')
        if codes:
            where.append('s.code IN ({})'.format(','.join(['?']*len(codes)))); params.extend(codes)
        sql='''
            SELECT s.trade_date AS 日期, s.snapshot_time AS 采样时间, s.code AS 代码,
                   COALESCE(sl.name,'') AS 名称, s.last_price AS 最后价,
                   s.day_open AS 日内开, s.day_high AS 日内高, s.day_low AS 日内低,
                   s.pct_chg AS 日内涨跌幅, s.volume AS 成交量, s.amount AS 成交额,
                   s.latest_bar_time AS 最新5分钟, s.source AS 数据源
            FROM intraday_snapshots s LEFT JOIN stock_list sl ON sl.code=s.code
            WHERE ''' + ' AND '.join(where) + '''
            ORDER BY s.snapshot_time DESC, s.code
        '''
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql_query(sql, conn, params=params)

    def query_intraday_fetch_log(self, trade_date, codes=None):
        codes=[normalize_code(c) for c in (codes or [])]
        where, params=['trade_date=?'], [trade_date]
        if codes:
            where.append('code IN ({})'.format(','.join(['?']*len(codes)))); params.extend(codes)
        sql='''SELECT fetched_at AS 获取时间, trade_date AS 日期, code AS 代码,
                       source AS 数据源, status AS 状态, latest_bar_time AS 最新5分钟,
                       error_message AS 错误信息
                FROM intraday_fetch_log WHERE '''+' AND '.join(where)+''' ORDER BY fetched_at DESC, code'''
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql_query(sql, conn, params=params)

    def get_previous_daily_close(self, code, trade_date):
        code = normalize_code(code)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('''
                SELECT close FROM daily_data
                WHERE code=? AND adjust_flag='raw' AND trade_date<?
                ORDER BY trade_date DESC LIMIT 1
            ''', (code, trade_date)).fetchone()
        return safe_float(row[0]) if row else None

    # ---------- 日线读取 ----------
    def load_screen_data(self, end_date, calendar_days=LOAD_CALENDAR_DAYS):
        start_date = (parse_date(end_date) - dt.timedelta(days=calendar_days)).strftime('%Y-%m-%d')
        with sqlite3.connect(self.db_path) as conn:
            q = pd.read_sql_query('''
                SELECT code, trade_date, open, high, low, close, volume, amount, pct_chg
                FROM daily_data
                WHERE adjust_flag='qfq' AND trade_date BETWEEN ? AND ?
                ORDER BY code, trade_date
            ''', conn, params=(start_date, end_date))
            r = pd.read_sql_query('''
                SELECT code, trade_date, open, high, low, close, volume, amount, pct_chg
                FROM daily_data
                WHERE adjust_flag='raw' AND trade_date BETWEEN ? AND ?
                ORDER BY code, trade_date
            ''', conn, params=(start_date, end_date))
        return self._transform_daily(q), self._transform_daily(r)

    @staticmethod
    def _transform_daily(source):
        result = {}
        if source.empty:
            return result
        source['code'] = source['code'].map(normalize_code)
        for code, group in source.groupby('code', sort=False):
            df = group.copy()
            df['日期'] = pd.to_datetime(df['trade_date'], errors='coerce')
            df = df.rename(columns={'open': '开盘', 'high': '最高', 'low': '最低', 'close': '收盘',
                                    'volume': '成交量', 'amount': '成交额', 'pct_chg': '涨跌幅'})
            df = df[['日期', '开盘', '最高', '最低', '收盘', '成交量', '成交额', '涨跌幅']]
            result[code] = df.dropna(subset=['日期', '收盘']).sort_values('日期').reset_index(drop=True)
        return result

    def load_chart_data(self, code, end_date, days=CHART_DAYS):
        code = normalize_code(code)
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query('''
                SELECT trade_date, open, high, low, close, volume, amount
                FROM daily_data WHERE code=? AND adjust_flag='qfq' AND trade_date<=?
                ORDER BY trade_date DESC LIMIT ?
            ''', conn, params=(code, end_date, days))
        if df.empty:
            return df
        return df.iloc[::-1].reset_index(drop=True)

    def query_watchlist_daily(self, codes, end_date):
        rows = []
        names = self.get_stock_list().set_index('code')['name'].to_dict()
        with sqlite3.connect(self.db_path) as conn:
            for code in codes:
                row = conn.execute('''
                    SELECT trade_date, open, high, low, close, volume, amount, pct_chg
                    FROM daily_data WHERE code=? AND adjust_flag='raw' AND trade_date<=?
                    ORDER BY trade_date DESC LIMIT 1
                ''', (code, end_date)).fetchone()
                if row:
                    rows.append({
                        '代码': code, '名称': names.get(code, ''), '日期': row[0],
                        '开盘': row[1], '最高': row[2], '最低': row[3], '收盘': row[4],
                        '成交量': row[5], '成交额(万)': round((row[6] or 0) / 10000, 2),
                        '涨跌幅%': row[7]
                    })
        return pd.DataFrame(rows)

    def query_watchlist_intraday(self, codes, trade_date=None):
        trade_date = trade_date or today_str()
        rows = []
        names = self.get_stock_list().set_index('code')['name'].to_dict()
        with sqlite3.connect(self.db_path) as conn:
            for code in codes:
                snaps = pd.read_sql_query('''
                    SELECT fetched_at, snapshot_time, last_price, day_open, day_high, day_low,
                           volume, amount, pct_chg, latest_bar_time
                    FROM intraday_snapshots
                    WHERE code=? AND trade_date=?
                    ORDER BY fetched_at DESC LIMIT 1
                ''', conn, params=(code, trade_date))
                if not snaps.empty:
                    r = snaps.iloc[0]
                    rows.append({
                        '代码': code, '名称': names.get(code, ''), '采样时间': r['snapshot_time'],
                        '最后价': r['last_price'], '日内开': r['day_open'], '日内高': r['day_high'],
                        '日内低': r['day_low'], '日内涨跌幅%': r['pct_chg'],
                        '成交量': r['volume'], '成交额(万)': round((r['amount'] or 0) / 10000, 2),
                        '最新5分钟': r['latest_bar_time']
                    })
        return pd.DataFrame(rows)

    def query_watchlist_daily_history(self, codes, start_date, end_date):
        names = self.get_stock_list().set_index('code')['name'].to_dict()
        rows = []
        with sqlite3.connect(self.db_path) as conn:
            for code in codes:
                raw = pd.read_sql_query('''
                    SELECT trade_date, open, high, low, close, volume, amount, pct_chg
                    FROM daily_data WHERE code=? AND adjust_flag='raw' AND trade_date BETWEEN ? AND ?
                    ORDER BY trade_date
                ''', conn, params=(code, start_date, end_date))
                qfq = pd.read_sql_query('''
                    SELECT trade_date, close FROM daily_data
                    WHERE code=? AND adjust_flag='qfq' AND trade_date<=?
                    ORDER BY trade_date
                ''', conn, params=(code, end_date))
                if raw.empty or qfq.empty:
                    continue
                raw['trade_date'] = pd.to_datetime(raw['trade_date'])
                qfq['trade_date'] = pd.to_datetime(qfq['trade_date'])
                qclose = qfq.set_index('trade_date')['close']
                qclose = qclose.reindex(qclose.index.sort_values())
                ma20 = qclose.rolling(20).mean()
                ma60 = qclose.rolling(60).mean()
                dif, dea, hist = calculate_macd(pd.DataFrame({'收盘': qclose.reset_index(drop=True)}))
                qtmp = pd.DataFrame({'trade_date': qclose.index, 'MA20': ma20.values, 'MA60': ma60.values,
                                     'DIF': dif.values, 'DEA': dea.values, 'MACD柱': hist.values})
                qtmp['trade_date'] = pd.to_datetime(qtmp['trade_date'])
                raw = raw.merge(qtmp, on='trade_date', how='left')
                raw['量比'] = raw['volume'].shift(0) / raw['volume'].shift(1).rolling(5).mean()
                raw = raw.tail((pd.to_datetime(end_date)-pd.to_datetime(start_date)).days + 1)
                for _, r in raw.iterrows():
                    rows.append({
                        '代码': code, '名称': names.get(code,''), '日期': r['trade_date'].strftime('%Y-%m-%d'),
                        '收盘': round(float(r['close']),2), '涨跌幅%': round(float(r['pct_chg']),2) if pd.notna(r['pct_chg']) else '',
                        '成交量': round(float(r['volume']),0) if pd.notna(r['volume']) else '',
                        '成交额(万)': round(float(r['amount'])/10000,2) if pd.notna(r['amount']) else '',
                        '量比': round(float(r['量比']),2) if pd.notna(r['量比']) else '',
                        'MA20': round(float(r['MA20']),2) if pd.notna(r['MA20']) else '',
                        'MA60': round(float(r['MA60']),2) if pd.notna(r['MA60']) else '',
                        'DIF': round(float(r['DIF']),3) if pd.notna(r['DIF']) else '',
                        'DEA': round(float(r['DEA']),3) if pd.notna(r['DEA']) else '',
                        'MACD柱': round(float(r['MACD柱']),3) if pd.notna(r['MACD柱']) else '',
                    })
        return pd.DataFrame(rows)


    def query_watchlist_intraday_history(self, codes, start_date, end_date):
        names = self.get_stock_list().set_index('code')['name'].to_dict()
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query('''
                SELECT s.trade_date, s.snapshot_time, s.code, COALESCE(sl.name,'') name,
                       s.last_price, s.day_high, s.day_low, s.pct_chg, s.volume, s.amount, s.latest_bar_time
                FROM intraday_snapshots s LEFT JOIN stock_list sl ON sl.code=s.code
                WHERE s.code IN ({}) AND s.trade_date BETWEEN ? AND ?
                ORDER BY s.trade_date, s.snapshot_time, s.code
            '''.format(','.join(['?']*len(codes))), list(codes)+[start_date,end_date]) if codes else pd.DataFrame()
        if df.empty:
            return df
        df = df.rename(columns={'trade_date':'日期','code':'代码','name':'名称','snapshot_time':'采样时间','last_price':'最后价',
                                'day_high':'日内高','day_low':'日内低','pct_chg':'日内涨跌幅%','amount':'成交额(万)','latest_bar_time':'最新5分钟'})
        df['成交额(万)'] = df['成交额(万)'].fillna(0)/10000
        return df


    def get_intraday_series(self, code, trade_date):
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query('''
                SELECT bar_time, open, high, low, close, volume, amount, fetched_at
                FROM intraday_bars
                WHERE code=? AND trade_date=? AND frequency=?
                ORDER BY fetched_at DESC, bar_time
            ''', conn, params=(normalize_code(code), trade_date, INTRADAY_FREQUENCY))
        return df

    def save_screen_record(self, params, result_codes, screen_date, data_mode):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO screen_records(screen_time, screen_date, data_mode, params, result_codes, note)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), screen_date, data_mode,
                  json.dumps(params, ensure_ascii=False), json.dumps(result_codes, ensure_ascii=False), ''))
            conn.commit()


# ==================== 指标 ====================

def calculate_rsi(df, period=14):
    """RSI14，使用收盘价；仅用于排序/确认，不作为单独买入条件。"""
    close = pd.to_numeric(df['收盘'], errors='coerce')
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(50.0)
    return rsi


def calculate_kdj(df, n=9, k_period=3, d_period=3):
    """KDJ(9,3,3)，返回K/D/J序列。"""
    high = pd.to_numeric(df['最高'], errors='coerce')
    low = pd.to_numeric(df['最低'], errors='coerce')
    close = pd.to_numeric(df['收盘'], errors='coerce')
    low_n = low.rolling(n, min_periods=n).min()
    high_n = high.rolling(n, min_periods=n).max()
    rsv = ((close - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50.0)
    k = rsv.ewm(com=k_period-1, adjust=False).mean()
    d = k.ewm(com=d_period-1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k.fillna(50.0), d.fillna(50.0), j.fillna(50.0)


def indicator_state(qfq_df):
    """返回KDJ/RSI的解释性状态。"""
    out = {
        'RSI14': None, 'RSI状态': '未知',
        'K值': None, 'D值': None, 'J值': None, 'KDJ状态': '未知'
    }
    if qfq_df is None or qfq_df.empty or len(qfq_df) < 20:
        return out
    try:
        rsi = calculate_rsi(qfq_df, 14)
        k, d, j = calculate_kdj(qfq_df, 9, 3, 3)
        rv = float(rsi.iloc[-1]); kv = float(k.iloc[-1]); dv = float(d.iloc[-1]); jv = float(j.iloc[-1])
        out.update({'RSI14': rv, 'K值': kv, 'D值': dv, 'J值': jv})
        if rv >= 70:
            out['RSI状态'] = '偏热'
        elif rv <= 30:
            out['RSI状态'] = '偏弱/超卖'
        elif rv >= 55:
            out['RSI状态'] = '偏强'
        else:
            out['RSI状态'] = '中性'

        if kv > dv and jv >= kv:
            out['KDJ状态'] = '多头'
        elif kv < dv and jv <= kv:
            out['KDJ状态'] = '空头'
        else:
            out['KDJ状态'] = '纠结'
    except Exception:
        pass
    return out


def calculate_macd(df, fast=12, slow=26, signal=9):
    close = pd.to_numeric(df['收盘'], errors='coerce')
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = 2 * (dif - dea)
    return dif, dea, hist


def intraday_metrics(df, prev_close=None):
    """从5分钟线计算盘中研究指标。兼容数据库查询返回的英文列名和采集器返回的中文列名。"""
    if df is None or df.empty:
        return {}

    x = df.copy()
    # get_intraday_series() 从 SQLite 返回的是英文列名；历史/实时抓取 DataFrame 则使用中文。
    # 在指标层统一成中文，避免 KeyError: '收盘'。
    rename_map = {
        'bar_time': '时间',
        'open': '开盘',
        'high': '最高',
        'low': '最低',
        'close': '收盘',
        'volume': '成交量',
        'amount': '成交额',
        'fetched_at': '抓取时间',
    }
    x = x.rename(columns={k: v for k, v in rename_map.items() if k in x.columns})

    required_cols = ['时间', '开盘', '最高', '最低', '收盘', '成交量', '成交额']
    missing = [c for c in required_cols if c not in x.columns]
    if missing:
        logger.warning(f'盘中指标计算缺少字段：{missing}；实际字段={list(x.columns)}')
        return {}

    x['收盘'] = pd.to_numeric(x['收盘'], errors='coerce')
    x['开盘'] = pd.to_numeric(x['开盘'], errors='coerce')
    x['最高'] = pd.to_numeric(x['最高'], errors='coerce')
    x['最低'] = pd.to_numeric(x['最低'], errors='coerce')
    x['成交量'] = pd.to_numeric(x['成交量'], errors='coerce').fillna(0)
    x['成交额'] = pd.to_numeric(x['成交额'], errors='coerce').fillna(0)
    x = x.dropna(subset=['收盘']).reset_index(drop=True)
    if x.empty:
        return {}
    last = float(x.iloc[-1]['收盘'])
    day_open = safe_float(x.iloc[0]['开盘'])
    day_high = safe_float(x['最高'].max())
    day_low = safe_float(x['最低'].min())
    total_volume = float(x['成交量'].sum())
    total_amount = float(x['成交额'].sum())
    vwap = total_amount / total_volume if total_volume > 0 else None
    pct = ((last / prev_close) - 1) * 100 if prev_close and last else None
    from_open = ((last / day_open) - 1) * 100 if day_open else None
    from_high = ((last / day_high) - 1) * 100 if day_high else None
    bars5 = x.tail(5)
    bars_prev5 = x.iloc[-10:-5] if len(x) >= 10 else x.iloc[:-5]
    recent_vol = float(bars5['成交量'].sum()) if not bars5.empty else 0.0
    prev_vol = float(bars_prev5['成交量'].sum()) if not bars_prev5.empty else 0.0
    vol_accel = float(recent_vol / prev_vol) if prev_vol > 0 else 1.0

    # 5分钟动量：优先比较5根K线前；首批不足5根时退化到首根采样。
    if len(x) >= 6:
        base5 = safe_float(x.iloc[-6]['收盘'])
    else:
        base5 = safe_float(x.iloc[0]['收盘']) if len(x) >= 2 else None
    ret5 = ((last / base5) - 1) * 100 if base5 and base5 > 0 else 0.0
    above_vwap = bool(vwap and last >= vwap)
    return {
        '盘中最新价': last, '盘中涨跌幅%': pct, '开盘至今%': from_open,
        '距日内高点%': from_high, 'VWAP': vwap, '是否站上VWAP': above_vwap,
        '5分钟动量%': ret5, '5分钟量能加速': vol_accel,
        '日内成交额万': total_amount / 10000, '最新5分钟': str(x.iloc[-1]['时间'])
    }


def build_intraday_plan(metrics, daily_bias='中性', aggressive=False):
    """生成策略研究用的两套情景方案，不保证收益。所有卖出场景遵守A股T+1。"""
    p = metrics.get('盘中最新价')
    if not p:
        return {}
    vwap = metrics.get('VWAP')
    pct = metrics.get('盘中涨跌幅%')
    above = metrics.get('是否站上VWAP')
    ret5 = metrics.get('5分钟动量%')
    accel = metrics.get('5分钟量能加速')
    dist_high = metrics.get('距日内高点%')

    score = 50
    reasons = []
    if daily_bias == '偏强': score += 15; reasons.append('日线背景偏强')
    elif daily_bias == '偏弱': score -= 15; reasons.append('日线背景偏弱')
    if above: score += 10; reasons.append('价格站上VWAP')
    else: score -= 8; reasons.append('价格低于VWAP')
    if ret5 is not None and ret5 > 0: score += 8; reasons.append('近5根分钟线动量为正')
    elif ret5 is not None and ret5 < -1: score -= 8; reasons.append('短线动量偏弱')
    if accel is not None and accel > 1.2: score += 7; reasons.append('近期量能加速')
    if pct is not None and pct > 6: score -= 8; reasons.append('日内涨幅过热')
    if dist_high is not None and dist_high < -3: score -= 5; reasons.append('距日内高点回落明显')
    score = max(0, min(100, score))
    if score >= 75: state='偏强，可研究介入'
    elif score >= 60: state='中性偏强，等待回踩/确认'
    elif score >= 45: state='观察，不追高'
    else: state='偏弱，暂缓'
    ref = vwap if vwap else p
    if aggressive:
        entry_low = min(p, ref) * 0.997
        entry_high = max(p, ref) * 1.003
        stop = entry_low * 0.97
        tp1 = entry_high * 1.05
        tp2 = entry_high * 1.08
        hold = 'T+1可首次评估，预期观察2~5个交易日'
    else:
        entry_low = min(p, ref) * 0.99
        entry_high = max(p, ref) * 1.001
        stop = entry_low * 0.975
        tp1 = entry_high * 1.04
        tp2 = entry_high * 1.06
        hold = 'T+1先评估，预期观察3~8个交易日'
    return {
        '盘中策略评分': score, '盘中状态': state,
        '参考入场区间': f'{entry_low:.2f}~{entry_high:.2f}',
        '参考止损': f'{stop:.2f}',
        '参考止盈': f'{tp1:.2f} / {tp2:.2f}',
        '预计观察周期': hold,
        '策略依据': '、'.join(reasons) if reasons else '信号不足，继续观察'
    }


def build_intraday_recommendation(raw_df, qfq_df, metrics, params):
    # 盘中综合评分：日线策略匹配 + 盘中强弱；仅作为研究参考。
    score = 0.0
    reasons=[]
    daily_score = 0.0
    if raw_df is not None and not raw_df.empty and qfq_df is not None and not qfq_df.empty:
        daily_score = strict_match_score(raw_df, qfq_df, params)
    score += daily_score * 0.65
    p = metrics.get('盘中最新价') or 0
    vwap = metrics.get('VWAP')
    ret5 = metrics.get('5分钟动量%')
    accel = metrics.get('5分钟量能加速')
    pct = metrics.get('盘中涨跌幅%')
    if vwap and p >= vwap: score += 12; reasons.append('站上VWAP')
    if ret5 is not None and 0 < ret5 <= 2.5: score += 8; reasons.append('短线动量健康')
    if accel is not None and 1.10 <= accel <= 2.20: score += 7; reasons.append('量能温和加速')
    if pct is not None:
        if 0 <= pct <= 5: score += 8; reasons.append('日内涨幅未过热')
        elif pct > 7: score -= 12; reasons.append('日内涨幅过热')
        elif pct < -2: score -= 8; reasons.append('日内偏弱')
    score = round(max(0,min(100,score)),1)
    state = '优先研究' if score>=80 else ('重点观察' if score>=70 else ('等待确认' if score>=60 else '暂缓'))
    # 两套规则化价格情景；不使用当天收盘/未来价格。
    ref = vwap or p
    aggressive_low = min(p,ref)*0.998 if p else 0
    aggressive_high = max(p,ref)*1.002 if p else 0
    conservative_low = min(p,ref)*0.99 if p else 0
    conservative_high = max(p,ref)*1.001 if p else 0
    a_stop = aggressive_low*0.97 if aggressive_low else 0
    a_tp1 = aggressive_high*1.05 if aggressive_high else 0
    a_tp2 = aggressive_high*1.08 if aggressive_high else 0
    c_stop = conservative_low*0.975 if conservative_low else 0
    c_tp1 = conservative_high*1.04 if conservative_high else 0
    c_tp2 = conservative_high*1.06 if conservative_high else 0
    return {
        '盘中综合评分': score, '盘中状态': state, '策略依据':'、'.join(reasons[:5]),
        '激进入场': f'{aggressive_low:.2f}~{aggressive_high:.2f}' if p else '',
        '激进止损': f'{a_stop:.2f}' if a_stop else '',
        '激进止盈': f'{a_tp1:.2f}/{a_tp2:.2f}' if a_tp1 else '',
        '激进周期': 'T+1起评估，2~5日',
        '保守入场': f'{conservative_low:.2f}~{conservative_high:.2f}' if p else '',
        '保守止损': f'{c_stop:.2f}' if c_stop else '',
        '保守止盈': f'{c_tp1:.2f}/{c_tp2:.2f}' if c_tp1 else '',
        '保守周期': 'T+1起评估，3~8日',
    }


def count_limit_ups(raw_df, days=LOOKBACK_TRADING_DAYS, threshold=9.8):
    if raw_df is None or len(raw_df) < 2:
        return 0
    pct = pd.to_numeric(raw_df.tail(days)['涨跌幅'], errors='coerce')
    return int((pct >= threshold).sum())


def recent_max_gain(raw_df, days=10):
    if raw_df is None or raw_df.empty:
        return np.nan
    pct = pd.to_numeric(raw_df.tail(days)['涨跌幅'], errors='coerce').dropna()
    return float(pct.max()) if not pct.empty else np.nan


def recent_return(raw_df, days=20):
    if raw_df is None or len(raw_df) <= days:
        return np.nan
    close = pd.to_numeric(raw_df['收盘'], errors='coerce')
    base, last = close.iloc[-days - 1], close.iloc[-1]
    if pd.isna(base) or pd.isna(last) or base == 0:
        return np.nan
    return float((last / base - 1) * 100)


def calculate_volume_ratio(raw_df, lookback=5):
    if raw_df is None or len(raw_df) <= lookback:
        return 0.0
    volume = pd.to_numeric(raw_df['成交量'], errors='coerce')
    past_avg = volume.shift(1).rolling(lookback).mean().iloc[-1]
    today = volume.iloc[-1]
    if pd.isna(past_avg) or past_avg <= 0 or pd.isna(today):
        return 0.0
    return float(today / past_avg)


def volume_contraction(raw_df, short_window=5, long_window=20):
    if raw_df is None or len(raw_df) < long_window:
        return np.nan
    volume = pd.to_numeric(raw_df['成交量'], errors='coerce')
    s = volume.rolling(short_window).mean().iloc[-1]
    l = volume.rolling(long_window).mean().iloc[-1]
    if pd.isna(s) or pd.isna(l) or l <= 0:
        return np.nan
    return float(s / l)


def price_position(raw_df, days=120):
    if raw_df is None or len(raw_df) < days:
        return np.nan
    close = float(raw_df['收盘'].iloc[-1])
    low = float(raw_df['最低'].tail(days).min())
    high = float(raw_df['最高'].tail(days).max())
    if high <= low:
        return 0.5
    return float((close - low) / (high - low))


def check_price(raw_df, min_price, max_price):
    if raw_df is None or raw_df.empty:
        return False
    price = float(raw_df.iloc[-1]['收盘'])
    return min_price <= price < max_price


def check_recent_no_limit_up(raw_df, days=10, threshold=9.8):
    if raw_df is None or len(raw_df) < days:
        return True
    pct = pd.to_numeric(raw_df.tail(days)['涨跌幅'], errors='coerce')
    return not bool((pct >= threshold).any())


def check_recent_gain_limit(raw_df, days=10, max_gain=5.0):
    if raw_df is None or len(raw_df) < days:
        return True
    pct = pd.to_numeric(raw_df.tail(days)['涨跌幅'], errors='coerce').dropna()
    return pct.empty or bool((pct <= max_gain).all())


def check_trend_up(qfq_df, ma_mid=20, ma_long=60):
    if qfq_df is None or len(qfq_df) < ma_long + 5:
        return False
    close = pd.to_numeric(qfq_df['收盘'], errors='coerce')
    ma20 = close.rolling(ma_mid).mean()
    ma60 = close.rolling(ma_long).mean()
    vals = [close.iloc[-1], ma20.iloc[-1], ma60.iloc[-1], ma20.iloc[-6]]
    if any(pd.isna(v) for v in vals):
        return False
    return bool(vals[0] > vals[1] > vals[2] and vals[1] > vals[3])


def macd_state(qfq_df):
    """MACD基础状态；MACD距采用 DEA-DIF，死叉时为正。"""
    dif, dea, hist = calculate_macd(qfq_df)
    if len(dif) < 4:
        return {}
    d0, d1, d2 = map(float, [dif.iloc[-1], dif.iloc[-2], dif.iloc[-3]])
    e0, e1, e2 = map(float, [dea.iloc[-1], dea.iloc[-2], dea.iloc[-3]])
    h0, h1 = float(hist.iloc[-1]), float(hist.iloc[-2])
    dist0, dist1, dist2 = e0-d0, e1-d1, e2-d2
    return {
        'dif': d0, 'dea': e0, 'hist': h0,
        'gap': abs(d0-e0),
        'macd_distance': dist0,
        'macd_distance_prev': dist1,
        'macd_distance_prev2': dist2,
        'death_cross': bool(d0 < e0),
        'dif_up': bool(d0 > d1),
        'golden_cross': bool(d1 < e1 and d0 >= e0),
        'repair_1d': bool(d0 < e0 and dist0 < dist1),
        'repair_2d': bool(d0 < e0 and dist2 > dist1 > dist0),
        'hist_improving': bool(h0 > h1),
        'zero_zone': '下方' if d0 < 0 and e0 < 0 else ('上方' if d0 > 0 and e0 > 0 else '跨零轴'),
    }


def macd_repair_state(qfq_df):
    """V1.42核心：仍处死叉，但MACD距（DEA-DIF）正在改善。

    重点不是“离金叉有多近”，而是：
    - 死叉是否还存在
    - MACD距是否连续缩小
    - DIF是否向上
    - MACD柱是否同步改善

    返回的细分字段只供内部评分/筛选使用，不作为界面新增指标。
    """
    s = macd_state(qfq_df)
    if not s:
        return {
            'score': 0.0, 'is_repair': False, 'is_strong_repair': False,
            'repair_1d': False, 'repair_2d': False, 'shrink_pct_1d': np.nan,
            'dif_up': False, 'hist_improving': False,
            'state': '数据不足', 'reason': ''
        }
    if not s['death_cross']:
        return {
            'score': 8.0 if s.get('golden_cross') else 0.0,
            'is_repair': False, 'is_strong_repair': False,
            'repair_1d': False, 'repair_2d': False, 'shrink_pct_1d': np.nan,
            'dif_up': bool(s.get('dif_up')), 'hist_improving': bool(s.get('hist_improving')),
            'state': '已金叉' if s.get('golden_cross') else '非死叉',
            'reason': '当前已不属于死叉修复窗口'
        }

    d0, d1, d2 = s['macd_distance'], s['macd_distance_prev'], s['macd_distance_prev2']
    repair_2d = bool(d2 > d1 > d0)
    repair_1d = bool(d1 > d0)
    shrink_pct = ((d1 - d0) / abs(d1) * 100.0) if d1 != 0 else (100.0 if d0 < d1 else 0.0)

    score = 18.0
    reasons = ['仍处死叉']
    if repair_2d:
        score += 48.0
        reasons.append('MACD距连续两天缩小')
    elif repair_1d:
        score += 24.0
        if shrink_pct >= 4.0:
            score += 10.0
            reasons.append(f'MACD距单日明显缩小{shrink_pct:.1f}%')
        else:
            reasons.append('MACD距开始缩小')
    else:
        score -= 35.0
        reasons.append('MACD距重新扩大')

    dif_up = bool(s.get('dif_up'))
    hist_improving = bool(s.get('hist_improving'))
    if dif_up:
        score += 12.0
        reasons.append('DIF向上')
    if hist_improving:
        score += 8.0
        reasons.append('MACD柱改善')

    strong = bool(
        repair_2d or
        (repair_1d and (shrink_pct >= 4.0 or (dif_up and hist_improving)))
    )
    state = '死叉修复｜强' if strong and score >= 65 else (
        '死叉修复｜中' if repair_1d and score >= 45 else
        ('死叉修复｜弱' if repair_1d else '死叉未修复')
    )

    return {
        'score': float(np.clip(score, 0, 100)),
        'is_repair': repair_1d,
        'is_strong_repair': strong,
        'repair_1d': repair_1d,
        'repair_2d': repair_2d,
        'shrink_pct_1d': float(shrink_pct),
        'dif_up': dif_up,
        'hist_improving': hist_improving,
        'state': state,
        'reason': '；'.join(reasons)
    }




def macd_stage_state(qfq_df, tangle_ratio=0.35):
    """V1.44：识别“绿柱由深变浅 + 尚未缠线”的修复阶段。"""
    out = {
        'score': 0.0, 'state': '数据不足',
        'green_2d': False, 'green_3d': False,
        'hist_negative': False, 'gap_ratio': np.nan,
        'ma5_above': False, 'ma5_stable': False, 'ma5_rising': False,
        'ma5_score': 50.0, 'reason': ''
    }
    if qfq_df is None or qfq_df.empty or len(qfq_df) < 25:
        return out
    try:
        close = pd.to_numeric(qfq_df['收盘'], errors='coerce')
        dif, dea, hist = calculate_macd(qfq_df)
        h = pd.to_numeric(hist, errors='coerce').dropna().reset_index(drop=True)
        d = pd.to_numeric(dif, errors='coerce').dropna().reset_index(drop=True)
        e = pd.to_numeric(dea, errors='coerce').dropna().reset_index(drop=True)
        if len(h) < 4 or len(d) < 4 or len(e) < 4:
            return out
        h0,h1,h2,h3 = map(float, [h.iloc[-1],h.iloc[-2],h.iloc[-3],h.iloc[-4]])
        d0,d1 = float(d.iloc[-1]), float(d.iloc[-2])
        e0,e1 = float(e.iloc[-1]), float(e.iloc[-2])
        negative = bool(h0 < 0)
        green2 = bool(h2 < h1 < h0 < 0)
        green3 = bool(h3 <= h2 < h1 < h0 < 0)
        gaps = (d - e).abs()
        med20 = float(gaps.tail(min(20,len(gaps))).median()) if not gaps.empty else np.nan
        gap = abs(d0-e0)
        gap_ratio = (gap / med20) if med20 and med20 > 0 else np.nan

        ma5 = close.rolling(5).mean()
        c0 = float(close.iloc[-1]); c1 = float(close.iloc[-2])
        m0 = float(ma5.iloc[-1]); m1 = float(ma5.iloc[-2])
        ma5_above = bool(c0 >= m0)
        prev_above = bool(c1 >= m1)
        ma5_rising = bool(m0 > m1)
        ma5_stable = bool(ma5_above and prev_above)
        ma_dist = ((c0/m0)-1)*100 if m0 else 0.0
        if ma5_stable and ma5_rising: ma5_score = 100.0
        elif ma5_above and ma5_rising: ma5_score = 88.0
        elif ma5_above: ma5_score = 72.0
        elif ma_dist >= -0.8 and ma5_rising: ma5_score = 58.0
        elif ma_dist >= -1.5: ma5_score = 40.0
        else: ma5_score = 20.0

        score = 20.0 if negative else 0.0
        reasons=[]
        if green3:
            score += 40; reasons.append('MACD绿柱连续3日缩短')
        elif green2:
            score += 28; reasons.append('MACD绿柱连续2日缩短')
        elif negative and h0 > h1:
            score += 12; reasons.append('MACD绿柱开始缩短')
        else:
            score -= 12
        if d0 > d1:
            score += 10; reasons.append('DIF向上')
        if e0 >= e1 - abs(e1)*0.02:
            score += 4
        if np.isfinite(gap_ratio):
            if gap_ratio < tangle_ratio:
                score -= 28; reasons.append('快慢线过度缠绕')
            elif gap_ratio < 0.55:
                score -= 3; reasons.append('接近缠线区')
            elif gap_ratio <= 0.85:
                score += 10; reasons.append('修复空间仍在')
            elif gap_ratio <= 1.10:
                score += 6
            else:
                score -= 6
        score += (ma5_score-50.0)*0.20
        if ma5_stable and ma5_rising:
            reasons.append('股价站稳5日线')
        elif ma5_above and ma5_rising:
            reasons.append('股价站上5日线')
        state = '早期修复' if green3 and (not np.isfinite(gap_ratio) or gap_ratio >= tangle_ratio) else (
            '修复中' if green2 and (not np.isfinite(gap_ratio) or gap_ratio >= tangle_ratio) else (
                '临界缠绕' if np.isfinite(gap_ratio) and gap_ratio < tangle_ratio else '修复待确认'
            )
        )
        out.update({
            'score': float(np.clip(score,0,100)), 'state': state,
            'green_2d': green2, 'green_3d': green3, 'hist_negative': negative,
            'gap_ratio': float(gap_ratio), 'ma5_above': ma5_above,
            'ma5_stable': ma5_stable, 'ma5_rising': ma5_rising,
            'ma5_score': float(ma5_score), 'reason': '；'.join(reasons[:5])
        })
        return out
    except Exception as exc:
        logger.debug(f'MACD阶段识别失败：{exc}')
        return out


def turnover_rate_from_profile(raw_df, profile):
    """V1.44：用成交额/流通市值计算近似换手率，避免依赖额外接口。"""
    try:
        cap = safe_float((profile or {}).get('circulating_market_cap_billion'))
        if cap is None or cap <= 0 or raw_df is None or raw_df.empty:
            return np.nan
        amount = safe_float(raw_df.iloc[-1].get('成交额'))
        if amount is None or amount <= 0:
            return np.nan
        return float(amount / (cap * 1e8) * 100.0)
    except Exception:
        return np.nan


def sector_context_map(raw_dict, qfq_dict, profiles):
    """V1.44：基于同日全市场股票画像+日线，给候选补充所属板块环境。"""
    groups = {}
    for code, rdf in (raw_dict or {}).items():
        code = normalize_code(code)
        p = (profiles or {}).get(code, {})
        industry = str(p.get('industry') or '').strip()
        theme = str(p.get('theme') or '').strip()
        key = industry if industry and industry not in ('暂无同日行业数据','未分类') else theme
        if not key or key == '未分类':
            continue
        if rdf is None or rdf.empty:
            continue
        try:
            close = pd.to_numeric(rdf['收盘'], errors='coerce').dropna()
            pct = safe_float(rdf.iloc[-1].get('涨跌幅'))
            if pct is None and len(close)>=2 and close.iloc[-2]:
                pct = float((close.iloc[-1]/close.iloc[-2]-1)*100)
            ret5 = float((close.iloc[-1]/close.iloc[-6]-1)*100) if len(close)>=6 and close.iloc[-6] else np.nan
            qdf = (qfq_dict or {}).get(code)
            above_ma20 = np.nan
            above_ma5 = np.nan
            if qdf is not None and not qdf.empty:
                qc = pd.to_numeric(qdf['收盘'], errors='coerce')
                ma20=qc.rolling(20).mean(); ma5=qc.rolling(5).mean()
                above_ma20 = bool(qc.iloc[-1] >= ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else np.nan
                above_ma5 = bool(qc.iloc[-1] >= ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else np.nan
            groups.setdefault(key, []).append({'pct':pct,'ret5':ret5,'above_ma20':above_ma20,'above_ma5':above_ma5})
        except Exception:
            continue
    stats={}
    for key, rows in groups.items():
        f=pd.DataFrame(rows)
        pct=pd.to_numeric(f['pct'], errors='coerce').dropna()
        r5=pd.to_numeric(f['ret5'], errors='coerce').dropna()
        n=len(pct)
        if n==0: continue
        median_pct=float(pct.median())
        up_ratio=float((pct>0).mean())
        median_r5=float(r5.median()) if not r5.empty else np.nan
        ma20_ratio=float(pd.Series(f['above_ma20']).dropna().mean()) if pd.Series(f['above_ma20']).notna().any() else np.nan
        ma5_ratio=float(pd.Series(f['above_ma5']).dropna().mean()) if pd.Series(f['above_ma5']).notna().any() else np.nan
        score=50 + np.clip(median_pct,-3,3)*8 + (up_ratio-0.5)*45
        if pd.notna(median_r5): score += np.clip(median_r5,-10,10)*0.8
        score=float(np.clip(score,0,100))
        state='板块偏强' if score>=65 else ('板块震荡偏强' if score>=55 else ('板块震荡偏弱' if score>=45 else '板块偏弱'))
        stats[key]={'name':key,'score':score,'state':state,'median_pct':median_pct,'median_ret5':median_r5,'up_ratio':up_ratio,'sample':len(rows),'above_ma20_ratio':ma20_ratio,'above_ma5_ratio':ma5_ratio}
    return stats


def get_stock_sector_context(code, profiles, sector_stats):
    p=(profiles or {}).get(normalize_code(code),{})
    industry=str(p.get('industry') or '').strip()
    theme=str(p.get('theme') or '').strip()
    key=industry if industry and industry not in ('暂无同日行业数据','未分类') else theme
    return (sector_stats or {}).get(key, {'name':key or '板块未知','score':50.0,'state':'板块数据不足','median_pct':np.nan,'median_ret5':np.nan,'up_ratio':np.nan,'sample':0,'above_ma20_ratio':np.nan,'above_ma5_ratio':np.nan})


def check_macd_repair(qfq_df):
    return bool(macd_repair_state(qfq_df).get('is_repair'))


def check_macd_golden_cross(qfq_df, gap_threshold=0.02, mode='both'):
    """兼容旧调用；V1.42改为死叉修复判断，不再使用固定MACD距阈值。"""
    return check_macd_repair(qfq_df)


# ==================== 策略与推荐度 ====================
def _clip_score(value, low, high, reverse=False):
    if value is None or pd.isna(value):
        return 0.0
    if high <= low:
        return 0.0
    x = max(low, min(high, float(value)))
    ratio = (x - low) / (high - low)
    if reverse:
        ratio = 1 - ratio
    return ratio


def signal_day_price_state(raw_df):
    """V1.42：推荐日价格状态，用于防止把已经明显启动的股票排到前面。"""
    if raw_df is None or raw_df.empty:
        return {'pct': np.nan, 'state': '未知'}
    try:
        pct = safe_float(raw_df.iloc[-1].get('涨跌幅'))
        if pct is None or not np.isfinite(pct):
            close = pd.to_numeric(raw_df['收盘'], errors='coerce')
            if len(close) >= 2 and close.iloc[-2] not in (0, np.nan) and pd.notna(close.iloc[-1]):
                pct = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
        if pct is None or pd.isna(pct):
            return {'pct': np.nan, 'state': '未知'}
        if pct >= 4.0:
            state = '明显上涨｜不宜追'
        elif pct >= 2.5:
            state = '偏强上涨｜谨防追高'
        elif pct <= -4.0:
            state = '明显下跌｜谨防下跌未止'
        elif pct <= -2.5:
            state = '偏弱回落'
        elif pct <= 0.5:
            state = '低位震荡/轻微回落'
        else:
            state = '温和上涨'
        return {'pct': float(pct), 'state': state}
    except Exception:
        return {'pct': np.nan, 'state': '未知'}


def macd_repair_entry_quality(raw_df, qfq_df, market_ctx=None):
    """V1.42：综合判断“死叉修复但尚未明显追涨”的入场质量。"""
    mr = macd_repair_state(qfq_df)
    ps = signal_day_price_state(raw_df)
    score = 0.0
    reasons = []

    if mr.get('repair_2d'):
        score += 28
        reasons.append('死叉距连续两天改善')
    elif mr.get('repair_1d'):
        shrink = safe_float(mr.get('shrink_pct_1d'))
        if shrink is not None and shrink >= 4:
            score += 18
            reasons.append('死叉距单日明显改善')
        else:
            score += 8
            reasons.append('死叉距刚开始改善')

    if mr.get('dif_up'):
        score += 10
    if mr.get('hist_improving'):
        score += 8

    pct = ps.get('pct')
    if pct is not None and not pd.isna(pct):
        if -1.5 <= pct <= 1.5:
            score += 12
            reasons.append('推荐日未明显追涨')
        elif 1.5 < pct < 2.5:
            score += 4
        elif 2.5 <= pct < 4.0:
            score -= 8
            reasons.append('推荐日上涨偏快')
        elif pct >= 4.0:
            score -= 25
            reasons.append('推荐日上涨过快')
        elif -4.0 < pct < -1.5:
            score -= 4
        else:
            score -= 18
            reasons.append('推荐日跌幅较大')

    con = volume_contraction(raw_df)
    vr = calculate_volume_ratio(raw_df)
    if not pd.isna(con):
        if con <= 0.80:
            score += 12
            reasons.append('前期有明显缩量')
        elif con <= 0.95:
            score += 7
    if vr is not None:
        if 1.0 <= vr <= 1.5:
            score += 7
        elif 1.5 < vr <= 2.0:
            score += 3
        elif vr > 2.2:
            score -= 5
            reasons.append('当日放量偏激进')

    # RSI只看是否开始回升；KDJ只看K/D是否开始抬头。
    try:
        rsi_s = calculate_rsi(qfq_df)
        if len(rsi_s) >= 2 and pd.notna(rsi_s.iloc[-1]) and pd.notna(rsi_s.iloc[-2]):
            if rsi_s.iloc[-1] > rsi_s.iloc[-2]:
                score += 6
                reasons.append('RSI回升')
    except Exception:
        pass

    try:
        ks, ds, js = calculate_kdj(qfq_df)
        if len(ks) >= 2 and pd.notna(ks.iloc[-1]) and pd.notna(ks.iloc[-2]) and ks.iloc[-1] > ks.iloc[-2]:
            score += 4
        if len(ds) >= 2 and pd.notna(ds.iloc[-1]) and pd.notna(ds.iloc[-2]) and ds.iloc[-1] > ds.iloc[-2]:
            score += 4
        if len(js) and pd.notna(js.iloc[-1]) and js.iloc[-1] > 105:
            score -= 3
    except Exception:
        pass

    ms = safe_float((market_ctx or {}).get('score'))
    if ms is not None:
        if ms >= 60:
            score += 4
        elif ms < 40:
            score -= 4

    return float(np.clip(score, 0, 100)), ps


def limit_up_gene_state(raw_df, threshold=9.8, lookback=LOOKBACK_TRADING_DAYS):
    """V1.44：把涨停基因从“涨停次数”升级为历史弹性、近期密度、距离最近涨停
    以及历史涨停后3个交易日的延续能力。

    仅使用目标日及以前的数据，适合历史回测；不参与硬筛选，只进入评分。
    """
    empty = {
        'score': 50.0, 'count250': 0, 'count120': 0, 'count60': 0,
        'days_since_last': None, 'follow_samples': 0,
        'follow_hit3': np.nan, 'follow_hit5': np.nan,
        'follow_avg_high3': np.nan, 'follow_max_high3': np.nan,
        'state': '数据不足', 'reason': '涨停历史数据不足'
    }
    if raw_df is None or raw_df.empty or '涨跌幅' not in raw_df.columns:
        return empty
    try:
        df = raw_df.copy().tail(int(lookback)).reset_index(drop=True)
        if len(df) < 20:
            return empty
        pct = pd.to_numeric(df['涨跌幅'], errors='coerce')
        flags = (pct >= float(threshold)).fillna(False)
        c250 = int(flags.sum())
        c120 = int(flags.tail(min(120, len(flags))).sum())
        c60 = int(flags.tail(min(60, len(flags))).sum())

        last_idxs = np.flatnonzero(flags.to_numpy())
        days_since = int(len(df) - 1 - last_idxs[-1]) if len(last_idxs) else None

        # 历史涨停后3个交易日内的最高涨幅，相对涨停日收盘计算。
        follow = []
        for idx in last_idxs.tolist():
            if idx + 1 >= len(df):
                continue
            base = safe_float(df.iloc[idx].get('收盘'))
            if base is None or base <= 0:
                continue
            future = df.iloc[idx + 1:idx + 4]
            highs = pd.to_numeric(future.get('最高'), errors='coerce')
            if highs is None or highs.dropna().empty:
                continue
            mx = float((highs.max() / base - 1.0) * 100.0)
            if np.isfinite(mx):
                follow.append(mx)

        follow_arr = np.asarray(follow, dtype=float) if follow else np.asarray([], dtype=float)
        sample = int(len(follow_arr))
        hit3 = float(np.mean(follow_arr >= 3.0)) if sample else np.nan
        hit5 = float(np.mean(follow_arr >= 5.0)) if sample else np.nan
        avg_high3 = float(np.mean(follow_arr)) if sample else np.nan
        max_high3 = float(np.max(follow_arr)) if sample else np.nan

        # 长期基因：候选最低5次，15次封顶。
        history_component = 5.0 + 20.0 * _clip_score(c250, 5, 15)
        # 近期活跃度：60日3次、120日5次分别封顶。
        recent_component = 10.0 * _clip_score(c60, 0, 3) + 10.0 * _clip_score(c120, 0, 5)

        if days_since is None:
            recency_component = 0.0
        elif 10 <= days_since <= 20:
            recency_component = 10.0
        elif 21 <= days_since <= 45:
            recency_component = 9.0
        elif 46 <= days_since <= 90:
            recency_component = 6.0
        elif 91 <= days_since <= 180:
            recency_component = 3.0
        else:
            recency_component = 1.0

        # 与本策略目标直接相关：历史涨停后3日内能否继续出现+3/+5空间。
        if sample >= 3:
            follow_component = 18.0 * hit3 + 7.0 * hit5
            avg_component = 10.0 * _clip_score(avg_high3, 3.0, 8.0)
        else:
            # 样本不足保持中性，避免最后几个涨停事件把分数极端化。
            follow_component = 12.5
            avg_component = 5.0

        score = float(np.clip(history_component + recent_component + recency_component + follow_component + avg_component, 0, 100))
        state = '强涨停基因' if score >= 72 else ('中强涨停基因' if score >= 58 else ('一般涨停基因' if score >= 42 else '偏弱涨停基因'))
        reason_parts = [f'250日涨停{c250}次']
        if c60:
            reason_parts.append(f'近60日{c60}次')
        if days_since is not None:
            reason_parts.append(f'距最近涨停{days_since}日')
        if sample >= 3:
            reason_parts.append(f'历史涨停后3日≥3%命中{hit3*100:.0f}%')
            reason_parts.append(f'历史涨停后3日≥5%命中{hit5*100:.0f}%')

        return {
            'score': round(score, 1), 'count250': c250, 'count120': c120, 'count60': c60,
            'days_since_last': days_since, 'follow_samples': sample,
            'follow_hit3': hit3, 'follow_hit5': hit5,
            'follow_avg_high3': avg_high3, 'follow_max_high3': max_high3,
            'state': state, 'reason': '；'.join(reason_parts)
        }
    except Exception:
        return empty


def calculate_extreme_score(raw_df, qfq_df, params, market_ctx=None, profile=None, sector_ctx=None):
    """V1.44：候选池之上的独立“极度推荐”评分。对应人工看盘八维，不改变候选池。"""
    if raw_df is None or qfq_df is None or raw_df.empty or qfq_df.empty:
        return {'score':0.0,'reason':'数据不足','gene':{}}
    profile = profile or {}
    mr = macd_repair_state(qfq_df)
    stage = macd_stage_state(qfq_df, float(params.get('macd_tangle_ratio',0.35)))
    gene = limit_up_gene_state(raw_df, params.get('limit_up_threshold',9.8), LOOKBACK_TRADING_DAYS)
    con = volume_contraction(raw_df); vol = calculate_volume_ratio(raw_df)
    ret20 = recent_return(raw_df,20); pos120 = price_position(raw_df,120)
    ind = indicator_state(qfq_df)
    turnover = turnover_rate_from_profile(raw_df, profile)
    cap = safe_float(profile.get('circulating_market_cap_billion'))
    market_score = safe_float((market_ctx or {}).get('score')) or 50.0
    sec_score = safe_float((sector_ctx or {}).get('score')) or 50.0

    parts={}
    parts['macd']=float(np.clip(stage.get('score',mr.get('score',0)),0,100))
    parts['ma5']=float(stage.get('ma5_score',50))

    k=safe_float(ind.get('K值')); d=safe_float(ind.get('D值')); j=safe_float(ind.get('J值'))
    k2=k; d2=d
    try:
        ks,ds,js=calculate_kdj(qfq_df)
        k2=float(ks.iloc[-2]); d2=float(ds.iloc[-2]); j2=float(js.iloc[-2])
    except Exception:
        j2=None
    kdj=50.0
    if k is not None and d is not None:
        if k>d: kdj += 25
        elif k<d and j is not None and j<20: kdj += 8
        else: kdj -= 5
        if k2 is not None and k>k2: kdj += 15
        if d2 is not None and d>d2: kdj += 10
        if j is not None and j>105: kdj -= 20
    parts['kdj']=float(np.clip(kdj,0,100))

    rsi=safe_float(ind.get('RSI14'))
    rsi_score=50.0
    try:
        rs=calculate_rsi(qfq_df)
        r0=float(rs.iloc[-1]); r1=float(rs.iloc[-2])
        rsi=r0
        if 45<=r0<=68: rsi_score += 25
        elif 35<=r0<45: rsi_score += 12
        elif 68<r0<=75: rsi_score += 8
        elif r0>80: rsi_score -= 25
        elif r0<25: rsi_score += 5
        if r0>r1: rsi_score += 20
        else: rsi_score -= 5
    except Exception:
        pass
    parts['rsi']=float(np.clip(rsi_score,0,100))

    vol_score=50.0
    if con is not None and not pd.isna(con):
        if 0.65<=con<=0.90: vol_score += 20
        elif 0.55<=con<0.65: vol_score += 10
        elif con<0.45: vol_score -= 8
        elif con>1.10: vol_score -= 8
    if vol is not None and not pd.isna(vol):
        if 1.0<=vol<=1.6: vol_score += 25
        elif 1.6<vol<=2.0: vol_score += 8
        elif 0.8<=vol<1.0: vol_score += 4
        elif vol>2.2: vol_score -= 25
        elif vol<0.6: vol_score -= 10
    parts['volume']=float(np.clip(vol_score,0,100))

    pos_score=55.0
    if pos120 is not None and not pd.isna(pos120):
        if 0.20<=pos120<=0.65: pos_score=90
        elif 0.10<=pos120<0.20 or 0.65<pos120<=0.78: pos_score=75
        elif pos120>0.85: pos_score=35
        else: pos_score=60
    if ret20 is not None and not pd.isna(ret20):
        if ret20>20: pos_score-=15
        elif ret20>15: pos_score-=8
        elif -12<=ret20<=5: pos_score+=5
    parts['position']=float(np.clip(pos_score,0,100))

    cap_score=55.0
    if cap is not None:
        if cap<30: cap_score=35.0
        elif cap<80: cap_score=72.0
        elif cap<250: cap_score=88.0
        elif cap<500: cap_score=68.0
        else: cap_score=55.0
    parts['market_cap']=cap_score

    turnover_score=50.0
    if turnover is not None and not pd.isna(turnover):
        if 2.0<=turnover<=8.0: turnover_score=92.0
        elif 1.0<=turnover<2.0: turnover_score=72.0
        elif 8.0<turnover<=15.0: turnover_score=68.0
        elif 15.0<turnover<=25.0: turnover_score=48.0
        elif turnover>25: turnover_score=25.0
        else: turnover_score=40.0
    parts['turnover']=turnover_score
    parts['gene']=float(np.clip(gene.get('score',50),0,100))
    parts['context']=float(np.clip(0.50*market_score+0.50*sec_score,0,100))

    weights={
        'macd':float(params.get('extreme_macd_weight',24.0)),
        'ma5':float(params.get('extreme_ma5_weight',10.0)),
        'kdj':float(params.get('extreme_kdj_weight',7.0)),
        'rsi':float(params.get('extreme_rsi_weight',7.0)),
        'volume':float(params.get('extreme_volume_weight',10.0)),
        'position':float(params.get('extreme_position_weight',8.0)),
        'market_cap':float(params.get('extreme_market_cap_weight',6.0)),
        'turnover':float(params.get('extreme_turnover_weight',4.0)),
        'gene':float(params.get('extreme_limit_gene_weight',18.0)),
        'context':float(params.get('extreme_context_weight',6.0)),
    }
    total=sum(weights.values()) or 1.0
    score=sum(parts[k]*weights[k] for k in weights)/total
    reasons=[stage.get('state','MACD未知'), gene.get('state','涨停基因未知')]
    if stage.get('green_3d'): reasons.append('绿柱连续3日缩短')
    elif stage.get('green_2d'): reasons.append('绿柱连续2日缩短')
    if stage.get('ma5_stable'): reasons.append('站稳5日线')
    elif stage.get('ma5_above'): reasons.append('站上5日线')
    if con is not None and not pd.isna(con) and con<=0.90 and vol is not None and 1.0<=vol<=1.6:
        reasons.append('缩量后温和放量')
    if turnover is not None and not pd.isna(turnover): reasons.append(f'换手约{turnover:.1f}%')
    sec_name=(sector_ctx or {}).get('name','板块未知')
    sec_state=(sector_ctx or {}).get('state','板块数据不足')
    reasons.append(f'{sec_name}{sec_state}')
    if cap is not None and cap<30: reasons.append('极小盘风险扣分')
    if parts['rsi']>=70: reasons.append('RSI状态协调')
    if parts['kdj']>=70: reasons.append('KDJ状态协调')
    return {'score':round(float(np.clip(score,0,100)),1),'reason':'；'.join(reasons[:8]),'gene':gene,
            'macd_stage':stage,'turnover':turnover,'parts':{k:round(v,1) for k,v in parts.items()}}


def apply_extreme_picks(df, top_n=5):
    """V1.44：不减少候选池，只标记极度推荐 Top N。"""
    if df is None or df.empty: return df
    out=df.copy(); out['极度推荐']=''; out['极度推荐排名']=''
    n=max(1,int(top_n or 5))
    sort_cols=[c for c in ['极度推荐分','涨停基因分','推荐度','严格匹配度'] if c in out.columns]
    if not sort_cols: return out
    ordered=out.sort_values(sort_cols,ascending=[False]*len(sort_cols),na_position='last')
    for rank,idx in enumerate(ordered.head(n).index,1):
        out.at[idx,'极度推荐']='TOP5' if n==5 else f'TOP{n}'
        out.at[idx,'极度推荐排名']=rank
    return out

def calculate_recommend_score(raw_df, qfq_df, params):
    # 0~100分，仅用于候选股排序，不等于确定性买入信号。
    vol_ratio = calculate_volume_ratio(raw_df)
    contraction = volume_contraction(raw_df)
    ret20 = recent_return(raw_df, 20)
    pos120 = price_position(raw_df, 120)
    limits = count_limit_ups(raw_df, LOOKBACK_TRADING_DAYS, params['limit_up_threshold'])
    max10 = recent_max_gain(raw_df, params['recent_days'])
    trend_bonus = 15 if check_trend_up(qfq_df) else 0
    macd = macd_state(qfq_df)

    score = 0.0
    # 1. 历史强势基因：普通推荐度保持原始“涨停次数”口径；质量升级留给极度推荐层。
    score += 12 * _clip_score(limits, params['min_limit_ups'], max(params['min_limit_ups'] + 8, 10))
    # 2. 趋势 15分
    score += trend_bonus
    # 3. 量能：放量但不爆量 15分
    if vol_ratio >= 1:
        score += 15 * _clip_score(vol_ratio, 1.0, 2.5)
    # 4. 缩量 15分
    if not pd.isna(contraction):
        score += 15 * _clip_score(contraction, 0.55, 1.0, reverse=True)
    # 5. 20日涨幅：偏好小幅抬升，不偏好暴冲 10分
    if not pd.isna(ret20):
        if 0 <= ret20 <= 20:
            score += 10 * (1 - abs(ret20 - 10) / 10)
            score = max(score, 0)
    # 6. 位置 10分：偏低位而不是高位加速
    if not pd.isna(pos120):
        score += 10 * _clip_score(pos120, 0.15, 0.65, reverse=True)
    # 7. 近期温度 5分：近期涨得越少越好，但完全不动也不额外奖励
    if not pd.isna(max10):
        score += 5 * _clip_score(max10, 0, 6, reverse=True)
    # 8. MACD核心：死叉距修复 10分
    mr = macd_repair_state(qfq_df)
    score += 10 * (mr.get('score',0.0) / 100.0)

    # 9. RSI 5分：偏好中强但不过热；超买只做扣分，不一票否决
    ind = indicator_state(qfq_df)
    rsi = ind.get('RSI14')
    if rsi is not None:
        if 55 <= rsi <= 68:
            score += 5
        elif 45 <= rsi < 55 or 68 < rsi <= 72:
            score += 3
        elif rsi > 80 or rsi < 25:
            score += 0
        else:
            score += 1

    # 10. KDJ 5分：偏好K>D、J不过热；只做确认，不做硬门槛
    k, d, j = ind.get('K值'), ind.get('D值'), ind.get('J值')
    if k is not None and d is not None and j is not None:
        if k > d and 45 <= k <= 80 and j <= 100:
            score += 5
        elif k >= d:
            score += 3
        elif k < d and j < 20:
            score += 1

    return round(max(0.0, min(100.0, score)), 1)



def _market_stock_features(raw_dict):
    """基于全市场已有日线构建市场环境快照。只使用目标日及以前的数据。"""
    rows = []
    for code, df in raw_dict.items():
        if df is None or df.empty or len(df) < 2:
            continue
        try:
            close = pd.to_numeric(df['收盘'], errors='coerce')
            pct = safe_float(df.iloc[-1].get('涨跌幅'))
            if pct is None or not np.isfinite(pct):
                pct = ((close.iloc[-1] / close.iloc[-2]) - 1) * 100 if close.iloc[-2] else np.nan
            ret5 = ((close.iloc[-1] / close.iloc[-6]) - 1) * 100 if len(close) >= 6 and close.iloc[-6] else np.nan
            ret20 = ((close.iloc[-1] / close.iloc[-21]) - 1) * 100 if len(close) >= 21 and close.iloc[-21] else np.nan
            ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else np.nan
            rows.append({'代码': code, 'pct': pct, 'ret5': ret5, 'ret20': ret20, 'above_ma20': bool(pd.notna(ma20) and close.iloc[-1] > ma20)})
        except Exception:
            continue
    if not rows:
        return {'score': 50.0, 'state': '数据不足', 'advance_ratio': np.nan, 'median_pct': np.nan, 'median_ret5': np.nan, 'median_ret20': np.nan, 'above_ma20_ratio': np.nan}
    f = pd.DataFrame(rows)
    pct = pd.to_numeric(f['pct'], errors='coerce')
    r5 = pd.to_numeric(f['ret5'], errors='coerce')
    r20 = pd.to_numeric(f['ret20'], errors='coerce')
    advance_ratio = float((pct > 0).mean())
    median_pct = float(pct.median())
    median_ret5 = float(r5.median()) if r5.notna().any() else np.nan
    median_ret20 = float(r20.median()) if r20.notna().any() else np.nan
    above_ma20_ratio = float(f['above_ma20'].mean())
    # 0~100：综合当日涨跌家数、当日中位数、5日中位数、20日站MA20比例。
    score = 50.0
    score += (advance_ratio - 0.5) * 70.0
    score += np.clip(median_pct, -3, 3) * 5.0
    if pd.notna(median_ret5): score += np.clip(median_ret5, -10, 10) * 0.8
    if pd.notna(median_ret20): score += np.clip(median_ret20, -20, 20) * 0.3
    score += (above_ma20_ratio - 0.5) * 30.0
    score = float(np.clip(score, 0, 100))
    if score >= 65: state = '偏强，可积极寻找相对强势股'
    elif score >= 55: state = '震荡偏强，精选个股'
    elif score >= 45: state = '震荡，控制仓位并精选'
    elif score >= 35: state = '偏弱，只看逆势强势股'
    else: state = '弱势，优先防守，原则上不追高'
    return {'score': score, 'state': state, 'advance_ratio': advance_ratio, 'median_pct': median_pct,
            'median_ret5': median_ret5, 'median_ret20': median_ret20, 'above_ma20_ratio': above_ma20_ratio, 'sample_size': len(f)}



def calculate_startup_score(raw_df, qfq_df, market_ctx=None):
    """V1.42启动评分：优先寻找修复后尚未明显启动的股票。"""
    if raw_df is None or qfq_df is None or len(raw_df) < 60 or len(qfq_df) < 60:
        return None, '数据不足'
    try:
        close = pd.to_numeric(raw_df['收盘'], errors='coerce')
        vol = pd.to_numeric(raw_df['成交量'], errors='coerce').fillna(0)
        high = pd.to_numeric(raw_df['最高'], errors='coerce')
        c = float(close.iloc[-1])
        v5 = float(vol.tail(5).mean()); v20 = float(vol.tail(20).mean())
        vratio = float(vol.iloc[-1] / v5) if v5 > 0 else 1.0
        h20 = float(high.tail(20).max())
        dist = (h20 / c - 1) * 100 if c > 0 else 999

        score = 0.0
        reasons = []

        # 前期缩量是正面；当前温和量能优于极端爆量。
        if v20 > 0:
            shrink = v5 / v20
            if shrink <= 0.80:
                score += 22; reasons.append('近期缩量充分')
            elif shrink <= 0.90:
                score += 16; reasons.append('近期量能收缩')
            elif shrink <= 1.05:
                score += 8
        if 0.8 <= vratio <= 1.5:
            score += 12
        elif 1.5 < vratio <= 2.0:
            score += 5
        elif vratio > 2.2:
            score -= 5; reasons.append('当前放量偏激进')

        # 平台越近不再越奖励，避免把已经上涨到阻力位的股票推到前面。
        if 0 <= dist <= 3:
            score += 5
        elif 3 < dist <= 8:
            score += 12; reasons.append('处于平台下方可观察区')
        elif 8 < dist <= 15:
            score += 9
        elif dist > 20:
            score += 2

        qclose = pd.to_numeric(qfq_df['收盘'], errors='coerce')
        ma5 = qclose.rolling(5).mean(); ma10 = qclose.rolling(10).mean(); ma20 = qclose.rolling(20).mean()
        if len(ma5) >= 3 and ma5.iloc[-1] > ma5.iloc[-2] and ma10.iloc[-1] >= ma10.iloc[-2]:
            score += 9; reasons.append('短均线开始抬头')
        if qclose.iloc[-1] > ma20.iloc[-1]:
            score += 5

        mr = macd_repair_state(qfq_df)
        score += 28 * (mr.get('score', 0.0) / 100.0)
        if mr.get('repair_2d'):
            reasons.append('死叉距连续两天改善')
        elif mr.get('repair_1d'):
            reasons.append('死叉距开始改善')

        ind = indicator_state(qfq_df)
        rsi = safe_float(ind.get('RSI14'))
        if rsi is not None and rsi > 78:
            score -= 4; reasons.append('RSI偏热')

        try:
            rs = calculate_rsi(qfq_df)
            if len(rs) >= 2 and pd.notna(rs.iloc[-1]) and pd.notna(rs.iloc[-2]) and rs.iloc[-1] > rs.iloc[-2]:
                score += 6; reasons.append('RSI回升')
        except Exception:
            pass

        try:
            ks, ds, js = calculate_kdj(qfq_df)
            if len(ks) >= 2 and pd.notna(ks.iloc[-1]) and pd.notna(ks.iloc[-2]) and ks.iloc[-1] > ks.iloc[-2]:
                score += 4
            if len(ds) >= 2 and pd.notna(ds.iloc[-1]) and pd.notna(ds.iloc[-2]) and ds.iloc[-1] > ds.iloc[-2]:
                score += 4
            if len(js) and pd.notna(js.iloc[-1]) and js.iloc[-1] > 105:
                score -= 3
        except Exception:
            pass

        # 推荐日已经上涨，说明可能进入启动后段，明确降低。
        ps = signal_day_price_state(raw_df)
        pct = ps.get('pct')
        if pct is not None and not pd.isna(pct):
            if -1.5 <= pct <= 1.5:
                score += 8; reasons.append('推荐日未明显追涨')
            elif 1.5 < pct <= 2.5:
                score += 2
            elif 2.5 < pct < 4:
                score -= 8; reasons.append('推荐日上涨偏快')
            elif pct >= 4:
                score -= 20; reasons.append('推荐日明显追涨')
            elif pct <= -4:
                score -= 12; reasons.append('推荐日下跌偏大')

        if market_ctx:
            ms = safe_float(market_ctx.get('score'))
            if ms is not None:
                if ms >= 60:
                    score += 4
                elif ms < 35:
                    score -= 4

        score = round(float(np.clip(score, 0, 100)), 1)
        return score, '；'.join(reasons[:5]) or '启动信号一般'
    except Exception as exc:
        return None, f'启动评分异常：{exc}'


def shareholder_score_from_record(record):
    """根据已披露股东户数变化计算0~100的筹码集中度代理分。

    这是辅助因子而非真实‘散户人数’；缺失数据返回50中性分。
    股东户数下降通常视作筹码趋于集中，适度加分；上升则适度扣分。
    """
    if not record:
        return 50.0
    change = safe_float(record.get('holder_change_pct'))
    if change is None or not np.isfinite(change):
        return 50.0
    # 映射到约20~80区间，避免季度数据对日线技术面权重过大。
    # -15%及以下 => 80；+15%及以上 => 20；中间线性。
    score = 50.0 - float(change) * 2.0
    return float(max(20.0, min(80.0, score)))




def calculate_chip_structure_index(raw_df):
    """基于日线量价行为估算0~100的筹码结构指数。
    注意：这不是实时散户/主力真实持仓比例，而是可回测的量价代理指标。
    """
    if raw_df is None or raw_df.empty:
        return None

    try:
        close = pd.to_numeric(raw_df['收盘'], errors='coerce')
        vol = pd.to_numeric(raw_df['成交量'], errors='coerce')
        high = pd.to_numeric(raw_df['最高'], errors='coerce')
        low = pd.to_numeric(raw_df['最低'], errors='coerce')

        if len(close) < 20:
            return None

        vol5 = float(vol.tail(5).mean())
        vol20 = float(vol.tail(20).mean())
        vol60 = float(vol.tail(60).mean()) if len(vol) >= 60 else vol20
        today_vol = float(vol.iloc[-1])

        price = float(close.iloc[-1])
        high20 = float(high.tail(20).max())
        low20 = float(low.tail(20).min())

        # 因子1：近期是否缩量
        contraction = (1 - vol5 / vol20) if vol20 > 0 else 0
        contraction_score = np.clip(50 + contraction * 80, 20, 80)

        # 因子2：今日是不是“温和放量”而非连续爆量
        today_ratio = today_vol / vol5 if vol5 > 0 else 1.0
        if 1.05 <= today_ratio <= 1.8:
            volume_change_score = 75
        elif 0.85 <= today_ratio < 1.05:
            volume_change_score = 55
        elif today_ratio > 1.8:
            volume_change_score = 45
        else:
            volume_change_score = 50

        # 因子3：价格是否处于近20日上半区，但离顶部不过度远
        range20 = high20 - low20
        if range20 > 0:
            pos = (price - low20) / range20
        else:
            pos = 0.5
        position_score = float(np.clip(pos * 100, 25, 85))

        # 因子4：近期量能是否持续失控
        stability = vol5 / vol60 if vol60 > 0 else 1.0
        stability_score = float(np.clip(70 - abs(stability - 0.9) * 45, 30, 75))

        score = (
            contraction_score * 0.35
            + volume_change_score * 0.25
            + position_score * 0.20
            + stability_score * 0.20
        )

        return round(float(np.clip(score, 0, 100)), 1)
    except Exception:
        return None


def build_chip_structure_state(raw_df):
    """生成界面展示文字，不虚构散户/主力占比。"""
    score = calculate_chip_structure_index(raw_df)
    if score is None:
        return '暂无筹码结构数据'

    if score >= 75:
        level = '筹码结构偏优'
    elif score >= 60:
        level = '筹码结构较好'
    elif score >= 45:
        level = '筹码结构中性'
    elif score >= 30:
        level = '筹码结构偏拥挤'
    else:
        level = '筹码结构偏弱'

    return f'{score:.0f}/100｜{level}'


# ==================== 行业/市值辅助模块 ====================
INDUSTRY_THEME_RULES = [
    ('计算机', ['软件开发','计算机设备','IT服务','互联网','人工智能']),
    ('电子', ['半导体','电子元件','消费电子','光学光电子','电子']),
    ('通信', ['通信服务','通信设备','光纤','通信']),
    ('电力设备', ['电力设备','电网设备','电机','电源设备','储能','光伏设备','风电设备']),
    ('机械设备', ['机械设备','专用设备','通用设备','自动化设备','工业母机','机床']),
    ('汽车', ['汽车整车','汽车零部件','汽车服务']),
    ('有色金属', ['有色金属','工业金属','贵金属','小金属','稀土']),
    ('煤炭', ['煤炭']),
    ('钢铁', ['钢铁']),
    ('基础化工', ['基础化工','化学原料','化学制品','化学纤维','农化制品','塑料']),
    ('医药生物', ['医药生物','化学制药','中药','生物制品','医疗器械','医疗服务']),
    ('食品饮料', ['食品饮料','食品加工制造','饮料制造','白酒']),
    ('家用电器', ['家用电器','白色家电','黑色家电']),
    ('纺织服饰', ['纺织服饰','纺织制造','服装家纺']),
    ('商贸零售', ['商贸零售']),('银行', ['银行']),
    ('非银金融', ['证券','保险','多元金融']),('房地产', ['房地产']),
    ('建筑材料/装饰', ['建筑材料','建筑装饰','建筑工程']),
    ('交通运输', ['交通运输','物流']),('公用事业', ['公用事业','电力','燃气','水务']),
    ('农林牧渔', ['农林牧渔','种植业','林业','养殖业','农产品加工']),
    ('国防军工', ['国防军工','军工']),('传媒', ['传媒']),('环保', ['环保']),('美容护理', ['美容护理']),
    ('社会服务', ['社会服务','旅游','酒店']),('综合', ['综合'])
]

def _theme_from_industry(industry='', name=''):
    s=f'{industry or ""} {name or ""}'.lower()
    for theme, keys in INDUSTRY_THEME_RULES:
        if any(k.lower() in s for k in keys): return theme
    return '未分类'

def classify_stock_theme(name='', industry=''):
    s = f'{name or ""} {industry or ""}'.lower()
    for theme, keys in THEME_RULES:
        if any(k.lower() in s for k in keys):
            return theme
    return '未分类'

def market_cap_score(circulating_cap_billion):
    v = safe_float(circulating_cap_billion)
    if v is None:
        return None
    if v < 30:
        return 30.0
    if v < 80:
        return 82.0
    if v < 150:
        return 95.0
    if v < 250:
        return 88.0
    if v < 500:
        return 65.0
    return 40.0

def market_cap_label(circulating_cap_billion):
    v = safe_float(circulating_cap_billion)
    if v is None:
        return '暂无同日流通市值'
    if v < 30:
        return f'{v:.1f}亿｜极小盘'
    if v < 80:
        return f'{v:.1f}亿｜高弹性'
    if v < 150:
        return f'{v:.1f}亿｜核心区'
    if v < 250:
        return f'{v:.1f}亿｜适中'
    if v < 500:
        return f'{v:.1f}亿｜偏大'
    return f'{v:.1f}亿｜大盘'

def build_stock_profile(name='', industry='', circulating_cap_billion=None):
    return {
        '行业/主题': classify_stock_theme(name, industry),
        '市值状态': market_cap_label(circulating_cap_billion),
        '市值适配分': market_cap_score(circulating_cap_billion),
    }

def build_volume_chip_state(raw_df):
    """基于真实行情数据描述量价结构；不冒充散户/主力真实占比。"""
    if raw_df is None or raw_df.empty:
        return '暂无量价筹码数据'
    try:
        vol = pd.to_numeric(raw_df['成交量'], errors='coerce')
        close = pd.to_numeric(raw_df['收盘'], errors='coerce')
        high = pd.to_numeric(raw_df['最高'], errors='coerce')

        vol5 = float(vol.tail(5).mean())
        vol20 = float(vol.tail(20).mean())
        today_vol = float(vol.iloc[-1])

        r520 = vol5 / vol20 if vol20 > 0 else np.nan
        r_today = today_vol / vol5 if vol5 > 0 else np.nan

        price = float(close.iloc[-1])
        high20 = float(high.tail(20).max())
        dist20 = (high20 / price - 1) * 100 if price > 0 else np.nan

        parts = []

        if pd.notna(r520):
            if r520 <= 0.75:
                parts.append(f'近期明显缩量（5/20量比{r520:.2f}）')
            elif r520 <= 0.90:
                parts.append(f'近期量能收缩（5/20量比{r520:.2f}）')
            else:
                parts.append(f'近期量能正常（5/20量比{r520:.2f}）')

        if pd.notna(r_today):
            if r_today >= 1.8:
                parts.append(f'今日放量偏快{r_today:.2f}倍')
            elif r_today >= 1.05:
                parts.append(f'今日温和放量{r_today:.2f}倍')
            else:
                parts.append(f'今日量能{r_today:.2f}倍5日均量')

        if pd.notna(dist20):
            if dist20 <= 3:
                parts.append(f'距20日高点仅{dist20:.1f}%')
            elif dist20 <= 8:
                parts.append(f'距20日高点{dist20:.1f}%')

        return '；'.join(parts[:3]) if parts else '量价状态一般'
    except Exception:
        return '量价状态一般'

def recommendation_reason(raw_df, qfq_df, params):
    """根据实际数值生成更自然、更有针对性的策略说明。"""
    if raw_df is None or qfq_df is None or raw_df.empty or qfq_df.empty:
        return '历史数据不足，暂时无法形成有效判断。'

    price = safe_float(raw_df.iloc[-1]['收盘']) or 0
    vol = calculate_volume_ratio(raw_df)
    con = volume_contraction(raw_df)
    ret5 = recent_return(raw_df, 5)
    ret20 = recent_return(raw_df, 20)
    pos120 = price_position(raw_df, 120)
    limits = count_limit_ups(raw_df, LOOKBACK_TRADING_DAYS, params['limit_up_threshold'])
    max10 = recent_max_gain(raw_df, params['recent_days'])
    macd = macd_state(qfq_df)
    close = pd.to_numeric(qfq_df['收盘'], errors='coerce')
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    high20 = pd.to_numeric(qfq_df['最高'], errors='coerce').tail(20).max()

    parts = []
    # 先讲结构，再讲量价，再讲MACD，避免机械词组堆叠。
    if pd.notna(ma20) and pd.notna(ma60):
        trend_spread = (ma20 / ma60 - 1) * 100 if ma60 else 0
        if price > ma5 > ma10 > ma20 > ma60:
            parts.append(f'当前收盘{price:.2f}，短中期均线呈多头排列，20日线高于60日线约{trend_spread:.1f}%，属于趋势保持较好的抬升结构')
        elif price > ma20 > ma60:
            parts.append(f'当前仍站在20日线之上，20日线相对60日线高约{trend_spread:.1f}%，但短线均线结构还不算特别强')
        else:
            parts.append('价格虽然进入候选，但均线结构并不算漂亮，后续更依赖实际承接而不是单纯趋势延续')

    if not pd.isna(ret20) and not pd.isna(ret5):
        if 3 <= ret20 <= 15 and ret5 <= 5:
            parts.append(f'近20日上涨约{ret20:.1f}%，近5日约{ret5:.1f}%，上涨速度相对温和，没有明显进入加速末端')
        elif ret20 > 20 or ret5 > 8:
            parts.append(f'近20日上涨约{ret20:.1f}%，短线约{ret5:.1f}%，已经偏热，继续追价的性价比下降')
        else:
            parts.append(f'近20日上涨约{ret20:.1f}%，更像整理后的早期抬升，方向性仍需后续确认')

    if not pd.isna(con) and not pd.isna(vol):
        if con < 0.80 and 1.0 <= vol <= 1.8:
            parts.append(f'5/20成交量比约{con:.2f}，前期有明显缩量，而当前量比约{vol:.2f}，属于“先缩量、再温和放量”的状态，这一点比较贴合当前策略')
        elif con < 0.90:
            parts.append(f'近期5/20成交量比约{con:.2f}，量能处于收缩状态，但当前量比约{vol:.2f}，启动力度还需要继续观察')
        else:
            parts.append(f'近期量能没有明显收缩，5/20成交量比约{con:.2f}，因此“底部缩量”的特征并不突出')

    if limits >= params['min_limit_ups']:
        limit_text = f'过去250个交易日有{limits}次涨停，历史弹性明显高于普通低价股'
        if not pd.isna(max10):
            limit_text += f'；近{params["recent_days"]}日最大单日涨幅约{max10:.1f}%'
        parts.append(limit_text)

    if macd:
        zone = macd.get('zero_zone', '未知')
        gap = macd.get('gap', np.nan)
        if macd.get('golden_cross'):
            parts.append(f'MACD刚出现日线金叉，DIF={macd["dif"]:.3f}、DEA={macd["dea"]:.3f}，位于{zone}区域，短线动能正在改善')
        elif macd.get('repair_2d'):
            parts.append(f'MACD仍处死叉，快慢线之间的MACD距已连续两天缩小，属于死叉修复状态，位于{zone}区域')
        elif macd.get('repair_1d'):
            parts.append(f'MACD仍处死叉，今天快慢线之间的MACD距开始缩小，属于死叉开始修复，位于{zone}区域')
        else:
            parts.append(f'MACD仍处{zone}区域，但死叉距暂未形成修复，继续观察')

    if not pd.isna(pos120) and not pd.isna(high20) and high20:
        dist_to_high = (high20 / price - 1) * 100 if price else 0
        if 0 <= pos120 <= 0.55:
            parts.append(f'当前处在近120日价格区间的约{pos120*100:.0f}%位置，距离近20日平台高点约{dist_to_high:.1f}%，尚未明显脱离底部区域')
        else:
            parts.append(f'当前位置约处于近120日区间{pos120*100:.0f}%位置，离阶段高位已经不算远，需要防止“看起来低价、实际上高位”')

    sh_rec = params.get('_shareholder_record')
    if params.get('shareholder_enabled', True) and sh_rec:
        h = safe_float(sh_rec.get('holder_count'))
        hp = safe_float(sh_rec.get('holder_change_pct'))
        rd = sh_rec.get('report_date', '')
        if hp is not None and h is not None:
            if hp <= -3:
                parts.append(f'最新披露股东户数约{h/10000:.2f}万，较上期减少{abs(hp):.1f}%，筹码集中度代理指标偏正面（披露截至{rd}）')
            elif hp >= 5:
                parts.append(f'最新披露股东户数约{h/10000:.2f}万，较上期增加{hp:.1f}%，筹码拥挤度偏高，短线更需等待承接（披露截至{rd}）')
            else:
                parts.append(f'最新披露股东户数约{h/10000:.2f}万，较上期变化{hp:.1f}%，筹码集中度代理指标中性（披露截至{rd}）')

    if not parts:
        return '基础条件通过，但当前数据没有形成特别强的单一优势，建议结合K线结构继续观察。'
    return '。'.join(parts[:5]) + '。'


def strict_match_score(raw_df, qfq_df, params):
    """在已经通过宽口径硬筛选的候选里做第二层严格评分。"""
    score = 0.0
    vol_ratio = calculate_volume_ratio(raw_df)
    contraction = volume_contraction(raw_df)
    ret20 = recent_return(raw_df, 20)
    pos120 = price_position(raw_df, 120)
    max10 = recent_max_gain(raw_df, params['recent_days'])
    limits = count_limit_ups(raw_df, LOOKBACK_TRADING_DAYS, params['limit_up_threshold'])
    gene = limit_up_gene_state(raw_df, params['limit_up_threshold'], LOOKBACK_TRADING_DAYS)
    macd = macd_state(qfq_df)

    # 价格：2~10 已由硬筛选保证；偏好 3~8 元，避免极低价。
    price = float(raw_df.iloc[-1]['收盘'])
    score += 8 if 3 <= price <= 8 else 5

    # 强势基因：继续保留次数，同时叠加涨停基因质量。
    score += min(18, 8 + max(0, limits - params['min_limit_ups']) * 2)
    score += 8.0 * (gene.get('score', 50.0) / 100.0)

    # 趋势只做辅助，避免“已经很强”压过死叉修复。
    if check_trend_up(qfq_df):
        score += 8

    # 缩量：5/20 越低越偏好，但过低可能失去活性。
    if not pd.isna(contraction):
        if 0.55 <= contraction <= 0.80:
            score += 14
        elif 0.80 < contraction <= 0.90:
            score += 10
        elif contraction < 0.55:
            score += 6

    # 温和上涨：偏好 5~15%，避免近期已经加速。
    if not pd.isna(ret20):
        if 5 <= ret20 <= 15:
            score += 12
        elif 0 <= ret20 < 5 or 15 < ret20 <= 20:
            score += 8

    # 近10日不热：最大单日涨幅越低越好，但过度沉寂也不加分。
    if not pd.isna(max10):
        if max10 <= 3.5:
            score += 8
        elif max10 <= 5.0:
            score += 6

    # 量能：温和优先，极端放量不追。
    if 0.9 <= vol_ratio <= 1.5:
        score += 7
    elif 1.5 < vol_ratio <= 2.0:
        score += 3
    elif vol_ratio > 2.2:
        score -= 4

    # MACD：死叉距连续缩小是核心。
    mr = macd_repair_state(qfq_df)
    score += 24 * (mr.get('score',0.0) / 100.0)
    if mr.get('repair_2d'):
        score += 4

    # V1.42：KDJ/RSI只作确认因子，不作为硬筛选，避免过度拟合。
    ind = indicator_state(qfq_df)
    rsi = ind.get('RSI14')
    k, d, j = ind.get('K值'), ind.get('D值'), ind.get('J值')
    if rsi is not None:
        if 55 <= rsi <= 68:
            score += 4
        elif 68 < rsi <= 75:
            score += 2
        elif rsi > 80:
            score -= 2
    if k is not None and d is not None and j is not None:
        if k > d and j <= 100:
            score += 4
        elif k < d and j < 20:
            score += 1
        elif k < d:
            score -= 1

    # V1.42：推荐日已经明显上涨时，降低排名；大跌也不把下跌未止误判成修复。
    ps = signal_day_price_state(raw_df)
    pct = ps.get('pct')
    if pct is not None and not pd.isna(pct):
        if -1.5 <= pct <= 1.5:
            score += 8
        elif 1.5 < pct <= 2.5:
            score += 1
        elif 2.5 < pct < 4.0:
            score -= 7
        elif pct >= 4.0:
            score -= 15
        elif pct <= -4.0:
            score -= 10

    # 平台位置过高时略扣，避免突破末端干扰修复策略。
    if not pd.isna(pos120) and pos120 > 0.80:
        score -= 6

    # V1.17：筹码集中度只做轻量加权，缺失数据不惩罚。
    sh_rec = params.get('_shareholder_record')
    if params.get('shareholder_enabled', True) and sh_rec:
        sh_score = shareholder_score_from_record(sh_rec)
        # 50 为中性，最多约 +4 / -3 分，不让季度指标压过量价与MACD。
        if sh_score is not None:
            score += max(-3.0, min(4.0, (sh_score - 50.0) * 0.12))

    return round(min(100.0, score), 1)



def atr_value(qfq_df, period=14):
    """基于前复权日线计算ATR，供交易计划做波动率参考。"""
    if qfq_df is None or len(qfq_df) < period + 1:
        return np.nan
    high = pd.to_numeric(qfq_df['最高'], errors='coerce')
    low = pd.to_numeric(qfq_df['最低'], errors='coerce')
    close = pd.to_numeric(qfq_df['收盘'], errors='coerce')
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1]) if pd.notna(tr.rolling(period).mean().iloc[-1]) else np.nan


def build_close_trade_plan(raw_df, qfq_df, params):
    """根据目标收盘日的实际结构生成动态交易计划。

    所有判断只使用目标日及之前的数据；卖出最早按T+1处理。
    持有周期不再写死，而是根据结构、ATR、目标空间动态估算。
    """
    if raw_df is None or qfq_df is None or raw_df.empty or qfq_df.empty:
        return {}

    close = float(raw_df.iloc[-1]['收盘'])
    high_today = float(raw_df.iloc[-1]['最高'])
    low_today = float(raw_df.iloc[-1]['最低'])
    q = qfq_df.copy()
    for c in ['收盘','最高','最低']:
        q[c] = pd.to_numeric(q[c], errors='coerce')
    ma5 = float(q['收盘'].rolling(5).mean().iloc[-1])
    ma10 = float(q['收盘'].rolling(10).mean().iloc[-1])
    ma20 = float(q['收盘'].rolling(20).mean().iloc[-1])
    ma60 = float(q['收盘'].rolling(60).mean().iloc[-1])
    high20 = float(q['最高'].tail(20).max())
    low20 = float(q['最低'].tail(20).min())
    atr14 = atr_value(q, 14)
    ret5 = recent_return(raw_df, 5)
    ret20 = recent_return(raw_df, 20)
    max10 = recent_max_gain(raw_df, params.get('recent_days', 10))
    con = volume_contraction(raw_df)
    vol_ratio = calculate_volume_ratio(raw_df)
    macd = macd_state(qfq_df)

    if any(pd.isna(v) for v in [ma5, ma10, ma20, ma60, high20, low20]):
        return {}
    if pd.isna(atr14) or atr14 <= 0:
        atr14 = max(close * 0.03, 0.01)

    near_breakout = close >= high20 * 0.965
    overheated = ((not pd.isna(max10)) and max10 > 7) or ((not pd.isna(ret5)) and ret5 > 8)
    trend_good = close > ma20 > ma60 and ma20 > q['收盘'].rolling(20).mean().iloc[-6]
    bullish_ma = close > ma5 > ma10 > ma20
    breakout_trigger = max(high20 * 1.003, high_today * 1.003)
    dist_to_resistance = ((high20 / close) - 1) * 100 if close else 0

    if overheated:
        state = '已经偏热，优先等回踩，不适合为了信号强行追价'
        setup = '过热回撤'
    elif near_breakout:
        state = '接近阶段平台高点，重点观察放量突破是否成立'
        setup = '突破临界'
    elif bullish_ma and not pd.isna(con) and con < 0.85:
        state = '趋势与量能结构较协调，更偏向“缩量整理后的再启动”'
        setup = '整理再启动'
    elif trend_good:
        state = '中期趋势仍在，但短线位置一般，更适合等待回踩确认'
        setup = '趋势回踩'
    elif close > ma20:
        state = '价格仍在20日线上方，但结构优势有限，等待进一步确认'
        setup = '弱趋势'
    else:
        state = '结构偏弱，不建议因为低价或历史涨停次数单独介入'
        setup = '偏弱'

    # 激进买点：围绕MA10/MA20和ATR寻找承接，不鼓励直接追收盘价。
    aggressive_low = max(ma10 * 0.995, ma20 - 0.35 * atr14)
    aggressive_high = min(close * 1.005, max(ma5, ma10) * 1.003)
    if aggressive_high < aggressive_low:
        aggressive_high = aggressive_low

    # 突破临界或接近前高时，保守方案用“确认后再买”，不是简单固定百分比。
    conservative_low = breakout_trigger
    conservative_high = breakout_trigger + max(0.20 * atr14, breakout_trigger * 0.004)

    # 如果离MA5太远，强制激进方案回归均线附近。
    chase_distance = (close / ma5 - 1) * 100 if ma5 else 0
    if chase_distance > 4:
        aggressive_high = min(aggressive_high, ma5 * 1.003)
        aggressive_low = min(aggressive_high, ma5 * 0.992)

    a_entry = (aggressive_low + aggressive_high) / 2
    c_entry = (conservative_low + conservative_high) / 2

    # 止损优先参考结构，再参考ATR；尽量与股票自己的波动匹配。
    a_stop = max(ma20 - 0.80 * atr14, a_entry - 1.05 * atr14)
    c_stop = max(ma20 - 0.65 * atr14, c_entry - 0.85 * atr14)
    if a_stop >= a_entry:
        a_stop = a_entry - 0.95 * atr14
    if c_stop >= c_entry:
        c_stop = c_entry - 0.80 * atr14
    a_stop = max(0.01, a_stop)
    c_stop = max(0.01, c_stop)

    # 止盈：先看前高，再看ATR延伸空间。
    resistance = high20
    a_tp1 = max(resistance, a_entry + 1.35 * atr14)
    a_tp2 = max(a_tp1, a_entry + 2.25 * atr14)
    c_tp1 = max(resistance, c_entry + 1.10 * atr14)
    c_tp2 = max(c_tp1, c_entry + 1.90 * atr14)

    def estimate_days(entry, target, atr):
        if entry <= 0 or target <= entry or atr <= 0:
            return 3
        target_pct = (target / entry - 1) * 100
        atr_pct = (atr / entry) * 100
        if atr_pct <= 0:
            return 5
        # 用约60%的ATR作为“可实现推进速度”的粗略研究估计。
        days = int(round(target_pct / max(atr_pct * 0.60, 0.2))) + 1
        return max(1, min(10, days))

    a_days = estimate_days(a_entry, a_tp1, atr14)
    c_days = estimate_days(c_entry, c_tp1, atr14)
    if setup == '突破临界':
        a_cycle = f'若突破成立，最早T+1观察，通常约{a_days}个交易日评估一次'
        c_cycle = f'等待突破确认后，最早T+1观察，预计{max(c_days,2)}~{min(c_days+2,10)}个交易日'
    elif setup == '过热回撤':
        a_cycle = f'先等回踩，若重新企稳最早T+1观察；预计{max(2,a_days-1)}~{min(a_days+1,7)}个交易日'
        c_cycle = f'等待重新站稳关键均线后，最早T+1观察；预计{max(3,c_days)}~{min(c_days+3,10)}个交易日'
    elif setup in ('整理再启动','趋势回踩'):
        a_cycle = f'回踩企稳后最早T+1观察；按ATR与目标空间预计{max(2,a_days-1)}~{min(a_days+1,8)}个交易日'
        c_cycle = f'确认后最早T+1观察；按当前波动率预计{max(3,c_days)}~{min(c_days+3,10)}个交易日'
    else:
        a_cycle = f'信号偏弱，建议只做{max(1,min(3,a_days))}个交易日以内的观察，不宜预设长期持仓'
        c_cycle = '等待趋势重新改善后再评估，当前不建立明确持有周期'

    # 更具体的“为什么这么安排”。
    narrative = []
    if setup == '整理再启动':
        narrative.append('当前形态最接近缩量整理后的再启动，而不是加速末端，因此激进方案放在均线/ATR承接区，避免直接追收盘价')
    elif setup == '突破临界':
        narrative.append(f'价格距离20日平台高点约{dist_to_resistance:.1f}%，已经进入突破敏感区，因此保守方案要求价格站稳平台上方，而不是提前猜突破')
    elif setup == '过热回撤':
        narrative.append(f'近期短线波动已经偏快（近10日最大单日涨幅约{max10:.1f}%）或5日涨幅过高，当前更需要等待换手/回踩')
    else:
        narrative.append(f'当前结构属于“{setup}”，因此计划重点是等待确认，而不是因为评分高就直接追价')

    if not pd.isna(con):
        narrative.append(f'5/20成交量比约{con:.2f}' + ('，说明前期有较明显的量能收缩' if con < 0.85 else '，量能收缩特征一般'))
    if not pd.isna(vol_ratio):
        narrative.append(f'今日量比约{vol_ratio:.2f}' + ('，存在温和放量' if 1 <= vol_ratio <= 1.8 else '，放量程度需要继续观察'))
    if macd:
        if macd.get('golden_cross'):
            narrative.append(f'MACD已经金叉，DIF与DEA差值约{macd.get("gap",0):.3f}，动能改善已经得到确认')
        elif macd.get('dif_up'):
            narrative.append(f'MACD仍处于金叉临界区，DIF正在向上靠拢DEA，差值约{macd.get("gap",0):.3f}')
        else:
            narrative.append('MACD仍缺乏持续向上确认，因此买点更偏向等待结构验证')

    return {
        '收盘交易状态': state,
        '交易结构': setup,
        '激进买点': f'{aggressive_low:.2f}~{aggressive_high:.2f}（承接/回踩）',
        '保守买点': f'{conservative_low:.2f}~{conservative_high:.2f}（突破确认）',
        '激进止损': f'{a_stop:.2f}',
        '保守止损': f'{c_stop:.2f}',
        '激进止盈': f'{a_tp1:.2f} / {a_tp2:.2f}',
        '保守止盈': f'{c_tp1:.2f} / {c_tp2:.2f}',
        '激进周期': a_cycle,
        '保守周期': c_cycle,
        '计划依据': '。'.join(narrative[:4]) + '。',
        'MA5': ma5, 'MA10': ma10, 'MA20': ma20, 'MA60': ma60,
        '20日平台高': high20, '20日低点': low20, 'ATR14': atr14,
        '今日收盘': close, '今日最高': high_today, '今日最低': low_today,
    }


def show_close_trade_plan_detail(parent, row):
    """弹出单只股票的收盘交易计划详情。"""
    win = tk.Toplevel(parent)
    win.title(f"{row.get('代码','')} {row.get('名称','')}｜收盘交易计划")
    win.geometry('760x650')
    win.transient(parent)
    win.grab_set()
    frm = ttk.Frame(win, padding=14)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frm, text=f"{row.get('代码','')} {row.get('名称','')}", font=UI_FONT_BOLD).pack(anchor=tk.W)
    ttk.Label(frm, text=f"收盘价：{row.get('最新价','')}    推荐度：{row.get('推荐度','')}    严格匹配度：{row.get('严格匹配度','')}", font=UI_FONT).pack(anchor=tk.W, pady=(5,10))

    sections = [
        ('当前判断', [('状态', row.get('收盘交易状态','')), ('依据', row.get('计划依据',''))]),
        ('激进方案', [
            ('参考买点', row.get('激进买点','')), ('止损', row.get('激进止损','')),
            ('止盈1 / 止盈2', row.get('激进止盈','')), ('周期', row.get('激进周期',''))
        ]),
        ('保守方案', [
            ('参考买点', row.get('保守买点','')), ('止损', row.get('保守止损','')),
            ('止盈1 / 止盈2', row.get('保守止盈','')), ('周期', row.get('保守周期',''))
        ]),
        ('关键位置', [
            ('MA5 / MA10', f"{row.get('MA5','')} / {row.get('MA10','')}"),
            ('MA20 / MA60', f"{row.get('MA20','')} / {row.get('MA60','')}"),
            ('20日平台高 / 20日低', f"{row.get('20日平台高','')} / {row.get('20日低点','')}"),
            ('ATR14', str(row.get('ATR14','')))
        ])
    ]
    for title, items in sections:
        lf = ttk.LabelFrame(frm, text=title, padding=10)
        lf.pack(fill=tk.X, pady=5)
        for k,v in items:
            rowf = ttk.Frame(lf); rowf.pack(fill=tk.X, pady=2)
            ttk.Label(rowf, text=f'{k}：', width=13, anchor=tk.E).pack(side=tk.LEFT)
            ttk.Label(rowf, text=str(v), wraplength=610, justify=tk.LEFT).pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Label(frm, text='说明：这是基于目标收盘日历史数据生成的规则化研究计划，不保证未来走势；卖出判断最早按T+1考虑。', foreground='#666').pack(anchor=tk.W, pady=8)
    ttk.Button(frm, text='关闭', command=win.destroy).pack(anchor=tk.E)




def _pick_col(df, names):
    for n in names:
        if n in df.columns:
            return n
    return None

def _normalize_em_cap(value):
    v=safe_float(value)
    if v is None or v<=0 or not np.isfinite(v): return None
    return float(v/1e8) if v>=1e7 else float(v)

def _ensure_profile_table(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_profile_cache (
            snapshot_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT, theme TEXT,
            total_market_cap REAL, circulating_market_cap REAL, total_share_capital REAL,
            source TEXT, fetched_at TEXT, PRIMARY KEY(snapshot_date,code))""")
        cols={r[1] for r in conn.execute('PRAGMA table_info(stock_profile_cache)').fetchall()}
        if 'total_share_capital' not in cols:
            conn.execute('ALTER TABLE stock_profile_cache ADD COLUMN total_share_capital REAL')
        conn.commit()

def cache_stock_profiles(db_path, df, snapshot_date=None):
    if df is None or df.empty: return 0
    snapshot_date=snapshot_date or today_str()
    code_col=_pick_col(df,['代码','code','证券代码']); name_col=_pick_col(df,['名称','name','证券简称'])
    industry_col=_pick_col(df,['所属行业','行业','industry']); total_col=_pick_col(df,['总市值','总市值(元)','总市值（元）','market_cap','total_market_cap'])
    circ_col=_pick_col(df,['流通市值','流通市值(元)','流通市值（元）','circulating_market_cap']); share_col=_pick_col(df,['总股本','总股本(股)','总股本（股）','总股本(万股)','total_share_capital'])
    if code_col is None:
        logger.warning(f'股票画像缺少代码列：{list(df.columns)[:30]}'); return 0
    rows=[]
    for _,r in df.iterrows():
        code=normalize_code(r.get(code_col,''))
        if not code or not code.startswith(('00','60')): continue
        name=str(r.get(name_col,'') if name_col else ''); industry=str(r.get(industry_col,'') if industry_col else '')
        theme=_theme_from_industry(industry,name)
        total=_normalize_em_cap(r.get(total_col)) if total_col else None; circ=_normalize_em_cap(r.get(circ_col)) if circ_col else None
        shares=safe_float(r.get(share_col)) if share_col else None
        if shares is not None and share_col and '万股' in str(share_col): shares*=10000
        rows.append((snapshot_date,code,name,industry,theme,total,circ,shares,df.attrs.get('source_label','公开行情画像'),dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    if not rows: return 0
    _ensure_profile_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany("""INSERT INTO stock_profile_cache
            (snapshot_date,code,name,industry,theme,total_market_cap,circulating_market_cap,total_share_capital,source,fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_date,code) DO UPDATE SET
            name=excluded.name,industry=excluded.industry,theme=excluded.theme,
            total_market_cap=excluded.total_market_cap,circulating_market_cap=excluded.circulating_market_cap,
            total_share_capital=excluded.total_share_capital,source=excluded.source,fetched_at=excluded.fetched_at""",rows)
        conn.commit()
    return len(rows)

def fetch_current_stock_profiles():
    """批量获取A股公开画像：东方财富公开列表接口优先，AKShare回退。"""
    import pandas as pd
    url='https://push2delay.eastmoney.com/api/qt/clist/get'
    params={'pn':1,'pz':10000,'po':1,'np':1,'fltt':2,'invt':2,
            'ut':'bd1d9ddb04089700cf9c27f6f7426281',
            'fs':'m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
            'fields':'f12,f14,f20,f21,f100,f101'}
    try:
        r=requests.get(url,params=params,timeout=15,headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})
        r.raise_for_status()
        data=r.json().get('data') or {}
        diff=data.get('diff') or []
        if isinstance(diff,dict): diff=list(diff.values())
        rows=[]
        for x in diff:
            code=normalize_code(x.get('f12'))
            if not code or not re.fullmatch(r'(00|60)\d{4}',code): continue
            rows.append({'代码':code,'名称':x.get('f14',''),
                         '总市值':safe_float(x.get('f20')),'流通市值':safe_float(x.get('f21')),
                         '所属行业':str(x.get('f100') or '').strip()})
        df=pd.DataFrame(rows)
        if not df.empty:
            df.attrs['source_label']='东方财富公开行情画像'
            logger.info(f'东方财富批量画像成功：{len(df)}只；行业=f100；总市值=f20；流通市值=f21')
            return df
    except Exception as exc:
        logger.warning(f'东方财富批量画像失败：{exc}')
    if ak is not None:
        try:
            df=ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                df=df.copy(); df.attrs['source_label']='AKShare-东方财富'
                logger.warning('东方财富批量画像失败，回退AKShare；若无行业字段不会伪造行业。')
                return df
        except Exception as exc:
            logger.warning(f'AKShare画像回退失败：{exc}')
    return pd.DataFrame()

def load_profile_map(db_path,target_date,codes):
    out={}; code_list=[normalize_code(c) for c in (codes or []) if normalize_code(c)]
    if not code_list: return out
    try:
        _ensure_profile_table(db_path); marks=','.join('?' for _ in code_list)
        with sqlite3.connect(db_path) as conn:
            rows=conn.execute(f"""SELECT code,name,industry,theme,total_market_cap,circulating_market_cap,total_share_capital,snapshot_date,source
                FROM stock_profile_cache WHERE snapshot_date=? AND code IN ({marks})""",[target_date]+code_list).fetchall()
        for r in rows:
            out[normalize_code(r[0])]={'name':r[1] or '','industry':r[2] or '','theme':r[3] or _theme_from_industry(r[2],r[1]),'total_market_cap_billion':safe_float(r[4]),'circulating_market_cap_billion':safe_float(r[5]),'total_share_capital':safe_float(r[6]),'snapshot_date':r[7],'source':r[8] or ''}
    except Exception as exc: logger.warning(f'读取股票画像缓存失败：{exc}')
    return out

def get_latest_profile_map(db_path,codes,asof_date=None):
    out={}; code_list=[normalize_code(c) for c in (codes or []) if normalize_code(c)]
    if not code_list: return out
    try:
        _ensure_profile_table(db_path); marks=','.join('?' for _ in code_list); asof=asof_date or today_str()
        with sqlite3.connect(db_path) as conn:
            rows=conn.execute(f"""SELECT p.code,p.name,p.industry,p.theme,p.total_market_cap,p.circulating_market_cap,p.total_share_capital,p.snapshot_date,p.source
                FROM stock_profile_cache p INNER JOIN (SELECT code,MAX(snapshot_date) md FROM stock_profile_cache WHERE snapshot_date<=? AND code IN ({marks}) GROUP BY code) x
                ON x.code=p.code AND x.md=p.snapshot_date""",[asof]+code_list).fetchall()
        for r in rows:
            out[normalize_code(r[0])]={'name':r[1] or '','industry':r[2] or '','theme':r[3] or _theme_from_industry(r[2],r[1]),'total_market_cap_billion':safe_float(r[4]),'circulating_market_cap_billion':safe_float(r[5]),'total_share_capital':safe_float(r[6]),'snapshot_date':r[7],'source':r[8] or ''}
    except Exception as exc: logger.warning(f'读取最新股票画像失败：{exc}')
    return out

def enrich_profiles_before_screen(data_manager, screen_date, codes):
    target = parse_date(screen_date).strftime('%Y-%m-%d')
    profiles = load_profile_map(data_manager.db_path, target, codes)

    # 当前日期：无条件尝试刷新一次公开画像，避免“空缓存”把刷新逻辑锁死。
    if target == today_str():
        live = fetch_current_stock_profiles()
        if not live.empty:
            saved = cache_stock_profiles(data_manager.db_path, live, target)
            logger.info(f'行业/市值画像刷新：API={len(live)}只，入库={saved}只')
            profiles = load_profile_map(data_manager.db_path, target, codes)

    valid_industry = sum(1 for p in profiles.values() if p.get('industry'))
    valid_total = sum(1 for p in profiles.values() if p.get('total_market_cap_billion') is not None)
    valid_circ = sum(1 for p in profiles.values() if p.get('circulating_market_cap_billion') is not None)
    logger.info(
        f'{target}画像匹配：{len(profiles)}/{len(codes)}；'
        f'行业={valid_industry}；总市值={valid_total}；流通市值={valid_circ}'
    )
    return profiles


def load_market_cap_profile(db_path,target_date,codes):
    return load_profile_map(db_path,target_date,codes)


def _relative_strength(raw_df, market_ctx=None):
    """1/5/20日相对全市场强度，严格只使用目标日及以前的数据。"""
    out = {'score': 50.0, 'rel1': np.nan, 'rel5': np.nan, 'rel20': np.nan}
    try:
        if raw_df is None or raw_df.empty:
            return out
        close = pd.to_numeric(raw_df['收盘'], errors='coerce').dropna()
        if len(close) < 2:
            return out
        r1 = ((close.iloc[-1] / close.iloc[-2]) - 1) * 100
        r5 = ((close.iloc[-1] / close.iloc[-6]) - 1) * 100 if len(close) >= 6 else r1
        r20 = ((close.iloc[-1] / close.iloc[-21]) - 1) * 100 if len(close) >= 21 else r5
        ctx = market_ctx or {}
        m1 = safe_float(ctx.get('median_pct')) or 0.0
        m5 = safe_float(ctx.get('median_ret5')) or 0.0
        m20 = safe_float(ctx.get('median_ret20')) or 0.0
        rel1 = float(r1 - m1); rel5 = float(r5 - m5); rel20 = float(r20 - m20)
        score = 50.0 + np.clip(rel1, -8, 8) * 1.2 + np.clip(rel5, -15, 15) + np.clip(rel20, -30, 30) * 0.45
        out.update(score=float(np.clip(score, 0, 100)), rel1=rel1, rel5=rel5, rel20=rel20)
    except Exception:
        pass
    return out


def table_exists_sqlite(conn, table_name):
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table_name,)).fetchone()
        return row is not None
    except Exception:
        return False


def _parse_entry_range(value):
    """从买入区间文本中提取上下限。"""
    if value is None:
        return (None, None)
    s = str(value).replace('～','~').replace('—','-').replace('至','~').strip()
    nums = __import__('re').findall(r'[-+]?\d+(?:\.\d+)?', s)
    try:
        vals = [float(x) for x in nums]
        if len(vals) >= 2:
            return min(vals[0], vals[1]), max(vals[0], vals[1])
        if len(vals) == 1:
            return vals[0], vals[0]
    except Exception:
        pass
    return (None, None)


def t1_opening_playbook(price, aggressive_range=None, conservative_range=None, params=None):
    """根据T日收盘买入区间生成T+1开盘观察建议。"""
    if price is None or price <= 0:
        return '暂无T+1开盘应对'
    ranges = [r for r in (aggressive_range, conservative_range) if r]
    highs = [r[1] for r in ranges if len(r) > 1 and r[1] is not None]
    hi = max(highs) if highs else price
    premium = (hi / price - 1) * 100
    if premium <= 1:
        return '平开/小幅高开：先观察开盘量价与承接，不急于追价'
    return '明显高开：不追高，优先等待回踩参考买入区并确认承接'


def build_result(code, name, raw_df, qfq_df, params, market_ctx=None, profile=None, sector_ctx=None):
    price = float(raw_df.iloc[-1]['收盘'])
    amount_raw = safe_float(raw_df.iloc[-1].get('成交额')) if hasattr(raw_df.iloc[-1], 'get') else None
    amount = amount_raw if amount_raw is not None else np.nan
    dif, dea, hist = calculate_macd(qfq_df)
    vol = calculate_volume_ratio(raw_df)
    con = volume_contraction(raw_df)
    ret20 = recent_return(raw_df, 20)
    limits = count_limit_ups(raw_df, LOOKBACK_TRADING_DAYS, params['limit_up_threshold'])
    max10 = recent_max_gain(raw_df, params['recent_days'])
    score = calculate_recommend_score(raw_df, qfq_df, params)
    sh_rec = params.get('_shareholder_record')
    sh_score = shareholder_score_from_record(sh_rec) if sh_rec else None
    strict_score = strict_match_score(raw_df, qfq_df, params)
    market_ctx = market_ctx or params.get('_market_context') or {'score':50.0,'state':'未知'}
    rs = _relative_strength(raw_df, market_ctx)
    profile = profile or {}
    cap_score = market_cap_score(profile.get('circulating_market_cap_billion', profile.get('total_market_cap_billion', profile.get('market_cap_billion'))))
    startup_score, startup_reason = calculate_startup_score(raw_df, qfq_df, market_ctx)
    timing_score, signal_day_state = macd_repair_entry_quality(raw_df, qfq_df, market_ctx)
    extreme = calculate_extreme_score(raw_df, qfq_df, params, market_ctx, profile=profile, sector_ctx=sector_ctx)
    gene = extreme.get('gene', {})
    # V1.44：极度推荐独立计算，不替换原推荐度。
    # V1.42：以“修复窗口质量”增强排序，同时主动压制推荐日追涨。
    startup_quality = startup_score if startup_score is not None else 50.0
    base_score = score
    # 启动评分作为核心排序增强项，但权重受控，避免完全替代原始策略。
    if startup_score is not None:
        score += (startup_score - 50.0) * 0.12
    score += (timing_score - 50.0) * 0.18
    if signal_day_state.get('pct') is not None and not pd.isna(signal_day_state.get('pct')):
        if signal_day_state['pct'] >= 4.0:
            score -= 10.0
        elif signal_day_state['pct'] >= 2.5:
            score -= 5.0
    if cap_score is not None and params.get('market_cap_weight', 0) > 0:
        score += (cap_score - 50.0) * (params.get('market_cap_weight', 8.0) / 50.0)
    if params.get('market_environment_enabled', True):
        # 市场弱时，只有相对强势股获得奖励；整体风险越高，普通候选越谨慎。
        market_adj = (market_ctx.get('score',50.0)-50.0) * (params.get('market_weight',12.0)/50.0)
        rs_adj = (rs.get('score',50.0)-50.0) * (params.get('relative_strength_weight',10.0)/50.0)
        score = float(np.clip(score + market_adj + rs_adj, 0, 100))
    plan = build_close_trade_plan(raw_df, qfq_df, params)
    macd = macd_state(qfq_df)
    ind = indicator_state(qfq_df)
    return {
        '代码': code,
        '名称': name,
        '行业/主题': f"{((profile or {}).get('industry','') or '暂无行业数据')} / {((profile or {}).get('theme','') or _theme_from_industry((profile or {}).get('industry',''), name))}",
        '所属行业': (profile or {}).get('industry','') or '暂无同日行业数据',
        '主题标签': (profile or {}).get('theme','') or _theme_from_industry((profile or {}).get('industry',''), name),
        '流通市值(亿)': round((profile or {}).get('circulating_market_cap_billion'), 2) if (profile or {}).get('circulating_market_cap_billion') is not None else '暂无',
        '参考总市值(亿)': round((profile or {}).get('total_market_cap_billion'), 2) if (profile or {}).get('total_market_cap_billion') is not None else '暂无',
        '市值状态': market_cap_label((profile or {}).get('circulating_market_cap_billion')) if (profile or {}).get('circulating_market_cap_billion') is not None else ('总市值可参考' if (profile or {}).get('total_market_cap_billion') is not None else '未取得市值'),
        '市值数据源': (profile or {}).get('source','') or '暂无',
        '市值快照日期': (profile or {}).get('snapshot_date','') or '暂无',
        '换手率': round(extreme.get('turnover'),2) if extreme.get('turnover') is not None and not pd.isna(extreme.get('turnover')) else '暂无',
        '板块名称': (sector_ctx or {}).get('name') or (profile or {}).get('industry') or (profile or {}).get('theme') or '板块未知',
        '板块状态': (sector_ctx or {}).get('state','板块数据不足'),
        '板块当日中位涨跌%': round((sector_ctx or {}).get('median_pct'),2) if (sector_ctx or {}).get('median_pct') is not None and not pd.isna((sector_ctx or {}).get('median_pct')) else '暂无',
        '板块5日中位涨跌%': round((sector_ctx or {}).get('median_ret5'),2) if (sector_ctx or {}).get('median_ret5') is not None and not pd.isna((sector_ctx or {}).get('median_ret5')) else '暂无',
        '板块上涨占比%': round((sector_ctx or {}).get('up_ratio')*100,1) if (sector_ctx or {}).get('up_ratio') is not None and not pd.isna((sector_ctx or {}).get('up_ratio')) else '暂无',
        '筹码结构': build_chip_structure_state(raw_df),
        '启动评分': startup_score if startup_score is not None else '',
        '启动判断': startup_reason,
        '极度推荐分': extreme.get('score', 0.0),
        '极度推荐理由': extreme.get('reason', ''),
        '涨停基因分': gene.get('score', 50.0),
        '涨停基因状态': gene.get('state', ''),
        '涨停基因说明': gene.get('reason', ''),
        '动能协同': f"RSI {ind.get('RSI状态','未知')}｜KDJ {ind.get('KDJ状态','未知')}｜MACD {'趋强' if macd.get('dif_up') else '中性'}",
        '推荐度': round(score, 1),
        '基础策略分': round(base_score, 1),
        '市场评分': round(market_ctx.get('score',50.0), 1),
        '市场状态': market_ctx.get('state','未知'),
        '相对强度': round(rs.get('score',50.0), 1),
        '相对大盘1日%': round(rs.get('rel1'), 2) if pd.notna(rs.get('rel1')) else '',
        '相对大盘5日%': round(rs.get('rel5'), 2) if pd.notna(rs.get('rel5')) else '',
        '相对大盘20日%': round(rs.get('rel20'), 2) if pd.notna(rs.get('rel20')) else '',
        '严格匹配度': strict_score,
        '严格评级': strict_score_reason(strict_score),
        '关注理由': recommendation_reason(raw_df, qfq_df, params),
        '最新价': round(price, 2),
        '今日涨跌%': round(signal_day_state.get('pct'), 2) if signal_day_state.get('pct') is not None and not pd.isna(signal_day_state.get('pct')) else '',
        '今日状态': signal_day_state.get('state', '未知'),
        'MA5状态': (extreme.get('macd_stage',{}) or {}).get('state','未知') + ('｜站稳5日线' if (extreme.get('macd_stage',{}) or {}).get('ma5_stable') else ('｜站上5日线' if (extreme.get('macd_stage',{}) or {}).get('ma5_above') else '｜未站稳5日线')),
        '收盘交易状态': plan.get('收盘交易状态', ''),
        '激进买点': plan.get('激进买点', ''),
        '保守买点': plan.get('保守买点', ''),
        '激进止损': plan.get('激进止损', ''),
        '保守止损': plan.get('保守止损', ''),
        '激进止盈': plan.get('激进止盈', ''),
        '保守止盈': plan.get('保守止盈', ''),
        '激进周期': plan.get('激进周期', ''),
        '保守周期': plan.get('保守周期', ''),
        '计划依据': plan.get('计划依据', ''),
        'T+1开盘应对': t1_opening_playbook(price, _parse_entry_range(plan.get('激进买点','')), _parse_entry_range(plan.get('保守买点','')), params),
        'MA5': round(plan['MA5'], 2) if plan.get('MA5') is not None else '',
        'MA10': round(plan['MA10'], 2) if plan.get('MA10') is not None else '',
        'MA20': round(plan['MA20'], 2) if plan.get('MA20') is not None else '',
        'MA60': round(plan['MA60'], 2) if plan.get('MA60') is not None else '',
        '20日平台高': round(plan['20日平台高'], 2) if plan.get('20日平台高') is not None else '',
        '20日低点': round(plan['20日低点'], 2) if plan.get('20日低点') is not None else '',
        'ATR14': round(plan['ATR14'], 3) if plan.get('ATR14') is not None else '',
        'DIF': round(float(dif.iloc[-1]), 3),
        'DEA': round(float(dea.iloc[-1]), 3),
        'MACD柱': round(float(hist.iloc[-1]), 3),
        'MACD零轴': macd.get('zero_zone', ''),
        'RSI14': round(float(ind.get('RSI14')), 2) if ind.get('RSI14') is not None else '',
        'RSI状态': ind.get('RSI状态',''),
        'K值': round(float(ind.get('K值')), 2) if ind.get('K值') is not None else '',
        'D值': round(float(ind.get('D值')), 2) if ind.get('D值') is not None else '',
        'J值': round(float(ind.get('J值')), 2) if ind.get('J值') is not None else '',
        'KDJ状态': ind.get('KDJ状态',''),
        '近一年涨停次数': limits,
        '量比': round(vol, 2),
        '5/20量比': round(con, 2) if not pd.isna(con) else '',
        '20日涨幅%': round(ret20, 2) if not pd.isna(ret20) else '',
        '近10日最大涨幅%': round(max10, 2) if not pd.isna(max10) else '',
        '成交额(万)': round(amount / 10000, 2) if pd.notna(amount) else '',
    }


def screen_stock(code, name, qfq_df, raw_df, params, market_ctx=None, profile=None, sector_ctx=None):
    if qfq_df is None or raw_df is None or qfq_df.empty or raw_df.empty:
        return None
    if len(raw_df) < 60 or len(qfq_df) < 60:
        return None
    if not check_price(raw_df, params['min_price'], params['max_price']):
        return None
    if calculate_volume_ratio(raw_df) < params['min_volume_ratio']:
        return None
    amount_raw = safe_float(raw_df.iloc[-1].get('成交额')) if hasattr(raw_df.iloc[-1], 'get') else None
    # 新浪历史K线常常没有可靠成交额字段。成交额缺失时，不应把整批股票误判为“不合格”。
    # 保留成交额列用于展示；只有存在有效成交额时才执行成交额下限。
    amount = amount_raw if amount_raw is not None else 0
    if amount_raw is not None and amount_raw < params['min_amount']:
        return None
    if count_limit_ups(raw_df, LOOKBACK_TRADING_DAYS, params['limit_up_threshold']) < params['min_limit_ups']:
        return None
    if not check_recent_no_limit_up(raw_df, params['recent_days'], params['limit_up_threshold']):
        return None
    if not check_recent_gain_limit(raw_df, params['recent_days'], params['max_daily_gain']):
        return None
    # V1.42：核心硬条件采用“2日连续修复优先；单日强修复放行”，
    # 避免V1.42在同一天大面积出现“刚缩一点点也入选”。
    mr = macd_repair_state(qfq_df)
    ps = signal_day_price_state(raw_df)
    if params.get('macd_repair_require_2d', True):
        if not mr.get('repair_2d'):
            one_day_strong = bool(
                mr.get('repair_1d') and
                safe_float(mr.get('shrink_pct_1d')) is not None and
                safe_float(mr.get('shrink_pct_1d')) >= params.get('macd_one_day_strong_shrink_pct', 4.0) and
                (mr.get('dif_up') or mr.get('hist_improving'))
            )
            if not one_day_strong:
                return None
    elif not mr.get('repair_1d'):
        return None

    pct = ps.get('pct')
    if pct is not None and not pd.isna(pct):
        if pct >= params.get('signal_day_chase_hard_pct', 4.0):
            return None
        if pct <= params.get('signal_day_drop_hard_pct', -4.0):
            return None

    # 弱市时，单日修复且相对弱势的股票再收紧，避免52只这种极端候选爆发。
    weak_ms = safe_float((market_ctx or {}).get('score'))
    if weak_ms is not None and weak_ms < params.get('macd_weak_market_score', 40.0) and not mr.get('repair_2d'):
        rel = _relative_strength(raw_df, market_ctx or {})
        rel5 = safe_float(rel.get('rel5'))
        if rel5 is None or rel5 < params.get('macd_weak_market_min_rel5', 0.0):
            return None

    # V1.42：普通RSI/KDJ状态继续交给评分系统。
    ind = indicator_state(qfq_df)
    rsi = safe_float(ind.get('RSI14'))
    k = safe_float(ind.get('K值')); d = safe_float(ind.get('D值'))
    if rsi is not None and rsi >= 85 and k is not None and d is not None and k < d:
        return None
    return build_result(code, name, raw_df, qfq_df, params, market_ctx, profile, sector_ctx)


def screen_stocks(data_manager, params, screen_date):
    stock_list=data_manager.get_stock_list()
    names=dict(zip(stock_list['code'],stock_list['name']))
    qfq,raw=data_manager.load_screen_data(screen_date)
    codes=list(qfq.keys())
    market_ctx=_market_stock_features(raw) if params.get('market_environment_enabled',True) else {'score':50.0,'state':'未启用'}
    profiles=enrich_profiles_before_screen(data_manager,screen_date,codes)
    sector_stats=sector_context_map(raw, qfq, profiles)
    results=[]; cap_filtered=0; cap_missing=0
    for code,qfq_df in qfq.items():
        local_params=dict(params); local_params['_shareholder_record']=None
        profile=dict(profiles.get(normalize_code(code),{}))
        cap=safe_float(profile.get('circulating_market_cap_billion'))
        if screen_date==today_str() and profile.get('total_market_cap_billion') is None and profile.get('total_share_capital') is not None:
            rdf=raw.get(code); close=safe_float(rdf.iloc[-1].get('收盘')) if rdf is not None and not rdf.empty else None
            if close and close>0: profile['total_market_cap_billion']=profile['total_share_capital']*close/1e8
        if params.get('market_cap_filter_enabled',False):
            # 历史回测只有目标日同日画像才允许做市值过滤，防止未来数据泄漏。
            if cap is None or profile.get('snapshot_date')!=str(screen_date):
                cap_missing+=1; continue
            lo=float(params.get('min_market_cap',0) or 0); hi=float(params.get('max_market_cap',999999) or 999999)
            if not (lo<=cap<=hi): cap_filtered+=1; continue
        result=screen_stock(code,names.get(code,''),qfq_df,raw.get(code),local_params,market_ctx,profile,get_stock_sector_context(code, profiles, sector_stats))
        if result: results.append(result)
    df=pd.DataFrame(results)
    if not df.empty:
        # V1.44：主列表仍按推荐度排序；另标记独立的极度推荐 Top 5。
        df=df.sort_values(['推荐度','严格匹配度','近一年涨停次数'],ascending=[False,False,False]).reset_index(drop=True)
        df=apply_extreme_picks(df, params.get('extreme_pick_count', 5))
        df.insert(0,'排名',range(1,len(df)+1))
    df.attrs['market_cap_filtered']=cap_filtered; df.attrs['market_cap_missing']=cap_missing
    return df


# ==================== GUI ====================
class StockResearchGUI:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry('1500x980')
        self.root.minsize(1250, 850)
        self.dm = DataManager()
        self.dm.mark_running_daily_jobs_interrupted()
        self.params = DEFAULT_PARAMS.copy()
        self.result_df = pd.DataFrame()
        self.current_date = today_str()
        self.current_mode = '收盘复盘'
        self.review_canvas = None
        self.watch_canvas = None
        self.review_mpl_toolbar = None
        self.watch_mpl_toolbar = None
        self.chart_days = CHART_DAYS
        self.chart_indicator_var = tk.StringVar(value='MACD')
        self.daily_update_running = False
        self.screen_running = False
        self.review_page = 1
        self.review_page_size = 10
        self.review_extreme_only = False
        self.backtest_daily_df = pd.DataFrame()
        self.backtest_detail_df = pd.DataFrame()
        self.backtest_running = False
        self.data_source_var = tk.StringVar(value=DATA_SOURCE_MODE)
        self.daily_summary_var = tk.StringVar(value='日线状态：尚未更新')
        self.build_ui()
        self.load_on_start()

    def ui(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def set_status(self, text):
        self.ui(self.status_var.set, text)

    def build_ui(self):
        # 使用命名 tkfont 对象设置 ttk 字体，避免 Tk 把带空格的字体字符串误解析。
        try:
            available = set(tkfont.families())
            ui_family = next((x for x in ['Microsoft YaHei', 'SimHei', 'SimSun', 'Arial Unicode MS'] if x in available), 'TkDefaultFont')
            self._default_font = tkfont.Font(family=ui_family, size=9)
            self._bold_font = tkfont.Font(family=ui_family, size=9, weight='bold')
            style = ttk.Style(self.root)
            style.configure('.', font=UI_FONT)
            style.configure('Treeview', font=UI_FONT, rowheight=24)
            style.configure('Treeview.Heading', font=UI_FONT_BOLD)
            style.configure('TNotebook.Tab', font=UI_FONT_BOLD)
            style.configure('TLabelframe.Label', font=UI_FONT_BOLD)
        except Exception:
            self._default_font = UI_FONT
            self._bold_font = UI_FONT_BOLD

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True)
        self.tab_review = ttk.Frame(self.nb)
        self.tab_intraday = ttk.Frame(self.nb)
        self.tab_watch = ttk.Frame(self.nb)
        self.tab_tracking = ttk.Frame(self.nb)
        self.tab_backtest = ttk.Frame(self.nb)
        self.nb.add(self.tab_review, text='① 收盘复盘 / 策略筛选')
        self.nb.add(self.tab_intraday, text='② 盘中采样')
        self.nb.add(self.tab_watch, text='③ 自选股监控')
        self.nb.add(self.tab_tracking, text='④ 推荐股跟踪')
        self.nb.add(self.tab_backtest, text='⑤ 历史策略回测')
        self.build_review_tab()
        self.build_intraday_tab()
        self.build_watch_tab()
        self.build_tracking_tab()
        self.build_backtest_tab()
        self.status_var = tk.StringVar(value='就绪')
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W).pack(fill=tk.X, padx=10, pady=2)

    # ---------- 复盘 ----------
    def build_review_tab(self):
        top = ttk.LabelFrame(self.tab_review, text='收盘复盘 / 选股参数', padding=8)
        top.pack(fill=tk.X, padx=8, pady=8)
        r0 = ttk.Frame(top); r0.pack(fill=tk.X, pady=3)
        ttk.Label(r0, text='复盘日期').pack(side=tk.LEFT, padx=4)
        self.review_date_var = tk.StringVar(value=today_str())
        ttk.Entry(r0, textvariable=self.review_date_var, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Label(r0, text='(YYYY-MM-DD)').pack(side=tk.LEFT, padx=4)
        self.review_progress = ttk.Progressbar(r0, length=180, mode='determinate')
        self.review_progress.pack(side=tk.LEFT, padx=8)
        self.review_progress_label = tk.StringVar(value='就绪')
        ttk.Label(r0, textvariable=self.review_progress_label).pack(side=tk.LEFT, padx=4)
        self.market_env_var = tk.StringVar(value='市场环境：待计算')
        ttk.Label(r0, textvariable=self.market_env_var).pack(side=tk.LEFT, padx=10)
        ttk.Button(r0, text='检查日期', command=self.review_check_date).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='校验股票主数据', command=self.repair_stock_master_ui).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='更新股东户数（自动补依赖）', command=self.update_shareholder_ui).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='获取/更新收盘数据', command=self.review_update_thread).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='执行策略筛选', command=self.run_screen).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='查看交易计划', command=self.review_plan_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='导出CSV', command=self.export_csv).pack(side=tk.LEFT, padx=5)
        ttk.Label(r0, text='数据源').pack(side=tk.LEFT, padx=(12,3))
        ttk.Combobox(r0, textvariable=self.data_source_var, values=['auto','baostock','sina','tushare','akshare'], width=10, state='readonly').pack(side=tk.LEFT, padx=3)
        ToolTip(r0, 'auto：BaoStock主源→新浪兜底→Tushare/AkShare可选后备；Tushare需要环境变量TUSHARE_TOKEN。')

        r1 = ttk.Frame(top); r1.pack(fill=tk.X, pady=3)
        self.vars = {}
        self._add_entry(r1, '最低价', 'min_price', 2.0, 7, '默认2元')
        self._add_entry(r1, '最高价', 'max_price', 10.0, 7, '默认10元')
        self._add_entry(r1, '涨停≥', 'min_limit_ups', 5, 7, '近250交易日')
        self._add_entry(r1, '近期天数', 'recent_days', 10, 7, '默认10日')
        self._add_entry(r1, '日涨幅≤', 'max_daily_gain', 5.0, 7, '近期单日涨幅')
        self._add_entry(r1, '量比≥', 'min_volume_ratio', 1.0, 7, '今日量/前5日均量')
        self._add_entry(r1, '成交额≥万', 'min_amount_wan', 500.0, 9, '成交额下限')
        self._add_entry(r1, '市值下限亿', 'min_market_cap', 0.0, 8, '0=不限制；启用后使用公开股票画像中的同日市值；历史日期不使用未来快照')
        self._add_entry(r1, '市值上限亿', 'max_market_cap', 999999.0, 9, '默认不限制，例如输入150可筛选150亿以下')
        ttk.Label(r1, text='市值筛选').pack(side=tk.LEFT, padx=4)
        self.market_cap_filter_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text='启用', variable=self.market_cap_filter_var).pack(side=tk.LEFT, padx=3)

        ttk.Label(r1, text='金叉模式').pack(side=tk.LEFT, padx=4)

        batch = ttk.LabelFrame(top, text='日线下载批次', padding=5)
        batch.pack(fill=tk.X, pady=5)
        self.review_batch_vars = []
        try:
            batches = self.dm.get_batches()
            for i, codes in enumerate(batches):
                var = tk.BooleanVar(value=True); self.review_batch_vars.append(var)
                text = f'批次{i+1} ({codes[0]}~{codes[-1]}, {len(codes)}只)' if codes else f'批次{i+1}(空)'
                ttk.Checkbutton(batch, text=text, variable=var).pack(side=tk.LEFT, padx=8)
        except Exception as exc:
            ttk.Label(batch, text=f'股票列表获取失败：{exc}').pack(side=tk.LEFT)

        body = ttk.PanedWindow(self.tab_review, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=5)
        table_frame = ttk.LabelFrame(body, text='策略候选（全量保留；默认每页10只；另设极度推荐 TOP 5）', padding=5)
        detail_frame = ttk.LabelFrame(body, text='选中股票详细分析｜左侧策略/交易计划 + 右侧K线', padding=5)
        body.add(table_frame, weight=4)
        body.add(detail_frame, weight=2)
        self.review_body = body
        self.review_chart_frame = detail_frame
        detail_pane = ttk.PanedWindow(detail_frame, orient=tk.HORIZONTAL)
        detail_pane.pack(fill=tk.BOTH, expand=True)
        detail_left = ttk.LabelFrame(detail_pane, text='策略与交易计划', padding=6)
        detail_right = ttk.LabelFrame(detail_pane, text='K线 / 成交量 / MACD', padding=6)
        detail_pane.add(detail_left, weight=1); detail_pane.add(detail_right, weight=2)
        self.review_detail_text = tk.Text(detail_left, wrap=tk.WORD, font=UI_FONT)
        self.review_detail_text.pack(fill=tk.BOTH, expand=True)
        self.review_detail_chart_frame = detail_right
        chart_bar=ttk.Frame(detail_right); chart_bar.pack(side=tk.TOP, fill=tk.X, pady=(0,4))
        ttk.Label(chart_bar,text='时间窗口').pack(side=tk.LEFT,padx=3)
        for text,val in [('60日',60),('120日',120),('250日',250),('全部',0)]:
            ttk.Button(chart_bar,text=text,command=lambda v=val:self.set_chart_window(v)).pack(side=tk.LEFT,padx=2)

        ttk.Label(chart_bar, text='指标').pack(side=tk.LEFT, padx=(12,3))
        for _label, _value in [('MACD','MACD'), ('KDJ','KDJ'), ('RSI','RSI'), ('全部','ALL')]:
            ttk.Radiobutton(
                chart_bar, text=_label, value=_value,
                variable=self.chart_indicator_var,
                command=self.refresh_current_review_chart
            ).pack(side=tk.LEFT, padx=2)
        ttk.Label(chart_bar,text='鼠标滚轮可缩放，下方工具栏可平移/缩放/保存').pack(side=tk.LEFT,padx=10)
        self.root.after(120, lambda: self._set_pane_position(body, 0.62))

        columns = ('排名','极度推荐','极度推荐分','代码','名称','推荐度','涨停基因分','启动评分','基础策略分','严格匹配度','最新价','今日涨跌%','今日状态','MA5状态','RSI14','KDJ状态','换手率','板块名称','板块状态','板块当日中位涨跌%','板块5日中位涨跌%','激进买点','保守买点','激进止损','保守止损','激进止盈','保守止盈','激进周期','保守周期','市场评分','市场状态','相对强度','相对大盘5日%','所属行业','主题标签','流通市值(亿)','参考总市值(亿)','市值状态','市值数据源','筹码结构','收盘交易状态','严格评级','关注理由')
        self.review_columns = columns
        # 策略候选：Treeview、纵向/横向滚动条全部放在同一容器，使用grid固定布局。
        tree_host = ttk.Frame(table_frame)
        tree_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        tree_host.grid_rowconfigure(0, weight=1)
        tree_host.grid_columnconfigure(0, weight=1)

        self.review_tree = ttk.Treeview(tree_host, columns=columns, show='headings')
        widths = {
            '排名':50,'极度推荐':78,'极度推荐分':88,'代码':72,'名称':95,'推荐度':72,'涨停基因分':82,'启动评分':80,'基础策略分':82,'严格匹配度':82,'最新价':75,'今日涨跌%':82,'今日状态':125,'MA5状态':105,'RSI14':72,'KDJ状态':90,'换手率':78,'板块名称':130,'板块状态':100,'板块当日中位涨跌%':110,'板块5日中位涨跌%':110,
            '激进买点':165,'保守买点':165,'激进止损':85,'保守止损':85,'激进止盈':115,'保守止盈':115,
            '激进周期':165,'保守周期':165,'市场评分':78,'市场状态':100,'相对强度':78,'相对大盘5日%':100,
            '行业/主题':190,'流通市值(亿)':105,'参考总市值(亿)':105,'市值状态':140,'筹码结构':220,
            '收盘交易状态':185,'严格评级':90,'关注理由':360
        }
        for c in columns:
            self.review_tree.heading(c, text=c)
            self.review_tree.column(c, width=widths.get(c,100), minwidth=widths.get(c,80), stretch=False, anchor=tk.CENTER)
        self.review_tree.column('关注理由', anchor=tk.W)

        review_y = ttk.Scrollbar(tree_host, orient=tk.VERTICAL, command=self.review_tree.yview)
        review_x = ttk.Scrollbar(tree_host, orient=tk.HORIZONTAL, command=self.review_tree.xview)
        self.review_tree.configure(yscrollcommand=review_y.set, xscrollcommand=review_x.set)
        self.review_tree.grid(row=0, column=0, sticky='nsew')
        review_y.grid(row=0, column=1, sticky='ns')
        review_x.grid(row=1, column=0, sticky='ew')

        self.review_top5_label = tk.StringVar(value='Top 5：尚未筛选')
        ttk.Label(table_frame, textvariable=self.review_top5_label).pack(side=tk.BOTTOM, anchor=tk.W, padx=5, pady=3)
        nav = ttk.Frame(table_frame)
        nav.pack(side=tk.BOTTOM, fill=tk.X, pady=2)
        ttk.Button(nav, text='上一页', command=lambda:self.change_review_page(-1)).pack(side=tk.LEFT, padx=3)
        self.review_page_label = tk.StringVar(value='第0/0页')
        ttk.Label(nav, textvariable=self.review_page_label).pack(side=tk.LEFT, padx=10)
        ttk.Button(nav, text='下一页', command=lambda:self.change_review_page(1)).pack(side=tk.LEFT, padx=3)
        ttk.Button(nav, text='仅看极度推荐 TOP 5', command=self.set_review_page_top5).pack(side=tk.LEFT, padx=8)
        ttk.Button(nav, text='显示全部候选', command=self.set_review_page_all).pack(side=tk.LEFT, padx=5)

        self.review_tree.bind('<<TreeviewSelect>>', self.review_select)
        self.review_tree.bind('<Double-1>', lambda event: self.review_plan_selected())

    def set_review_page_top5(self):
        self.review_extreme_only=True; self.review_page=1; self.display_review_results()

    def set_review_page_all(self):
        self.review_extreme_only=False; self.review_page=1; self.display_review_results()

    def _set_pane_position(self, pane, table_ratio=0.68):
        """初始化上下分栏比例，并保留用户后续拖动调整。"""
        try:
            h = pane.winfo_height()
            if h > 0:
                pane.sashpos(0, int(h * table_ratio))
        except tk.TclError:
            pass

    def _add_entry(self, parent, label, key, value, width, tip=''):
        ttk.Label(parent, text=label).pack(side=tk.LEFT, padx=3)
        var = tk.StringVar(value=str(value)); self.vars[key] = var
        e = ttk.Entry(parent, textvariable=var, width=width); e.pack(side=tk.LEFT, padx=3)
        if tip: ToolTip(e, tip)

    def read_params(self):
        try:
            return {
                'min_price': float(self.vars['min_price'].get()),
                'max_price': float(self.vars['max_price'].get()),
                'min_limit_ups': int(self.vars['min_limit_ups'].get()),
                'recent_days': int(self.vars['recent_days'].get()),
                'max_daily_gain': float(self.vars['max_daily_gain'].get()),
                'min_volume_ratio': float(self.vars['min_volume_ratio'].get()),
                'min_amount': float(self.vars['min_amount_wan'].get()) * 10000,
                'min_market_cap': max(0.0, float(self.vars['min_market_cap'].get())),
                'max_market_cap': max(0.0, float(self.vars['max_market_cap'].get())),
                'market_cap_filter_enabled': bool(self.market_cap_filter_var.get()),
                'limit_up_threshold': 9.8,
                'shareholder_enabled': bool(DEFAULT_PARAMS.get('shareholder_enabled', True)),
                'shareholder_weight': float(DEFAULT_PARAMS.get('shareholder_weight', 8.0)),
            }
        except Exception as exc:
            raise ValueError('筛选参数填写有误。') from exc

    def selected_review_codes(self):
        batches = self.dm.get_batches()
        return [code for i, batch in enumerate(batches) if i < len(self.review_batch_vars) and self.review_batch_vars[i].get() for code in batch]

    def refresh_market_profiles_thread(self):
        def worker():
            try:
                # V1.44：先走东方财富公开画像，不因为市值/行业刷新而强制安装AKShare。
                self.set_status('正在刷新行业/市值画像（公开接口优先）……')
                df=fetch_current_stock_profiles()
                if df.empty:
                    self.set_status('行业/市值刷新失败：公开接口无数据')
                    self.ui(messagebox.showwarning,'行业/市值数据','公开行情画像接口暂时没有返回数据；可稍后重试。')
                    return
                saved=cache_stock_profiles(self.dm.db_path,df,today_str())
                self.set_status(f'行业/市值刷新完成：{saved}只')
                self.ui(messagebox.showinfo,'刷新完成',f'已缓存{saved}只股票的行业/市值画像。\n重新执行功能1即可使用。')
            except Exception as exc:
                self.set_status(f'行业/市值刷新失败：{exc}')
                self.ui(messagebox.showwarning,'行业/市值数据',str(exc))
        threading.Thread(target=worker,daemon=True).start()

    def update_shareholder_ui(self):
        def worker():
            try:
                self.set_status('检查AKShare依赖环境……')
                ok, dep_msg = ensure_akshare_installed(quiet=True)
                if not ok:
                    self.ui(messagebox.showwarning, '股东户数模块暂不可用',
                            dep_msg + '\n\n不会影响功能1/2/3/4/5；你可以稍后重试，或手动安装AKShare。')
                    self.set_status('AKShare不可用：股东户数暂未启用')
                    return
                self.set_status(dep_msg + '，开始更新股东户数……')
                result = self.dm.update_shareholder_history()
                msg = f"季度快照：{result['quarters']}个\n入库：{result['saved']:,}条"
                if result['errors']:
                    msg += "\n失败季度：" + "、".join(x[0] for x in result['errors'])
                self.ui(messagebox.showinfo, '股东户数更新完成', msg + ('\n\n说明：股东户数为季度/公告数据，不是每日实时散户人数。' if result.get('saved', 0) > 0 else ''))
                self.set_status(f"股东户数更新完成：{result['saved']:,}条；缺失数据不会淘汰股票")
            except Exception as exc:
                self.ui(messagebox.showerror, '股东户数更新失败', str(exc))
                self.set_status('股东户数更新失败：' + str(exc)[:120])
        threading.Thread(target=worker, daemon=True).start()

    def repair_stock_master_ui(self):
        try:
            changes=self.dm.repair_stock_master_names()
            preview='\n'.join([f'{c}: {o} → {n}' for c,o,n in changes[:20]])
            messagebox.showinfo('股票主数据校验完成',f'修正{len(changes)}个名称。'+(f'\n\n{preview}' if preview else '\n无变更。'))
            self.set_status(f'股票主数据校验完成：修正{len(changes)}个名称。')
        except Exception as exc: messagebox.showerror('主数据校验失败',str(exc))

    def review_check_date(self):
        try:
            target = parse_date(self.review_date_var.get()).strftime('%Y-%m-%d')
        except ValueError:
            messagebox.showerror('错误', '日期必须是 YYYY-MM-DD。'); return
        total, q, r = self.dm.date_status(target)
        if is_before_close_for_today(target):
            messagebox.showwarning('尚未收盘', f'{target} 还没到15:05收盘复盘时间。\n当前只能使用前一个已完成交易日的数据。')
            return
        messagebox.showinfo('日期数据情况', f'{target}\n股票总数：{total}\n前复权：{q}\n不复权：{r}\n\n日线表与盘中表完全独立。')

    def review_update_thread(self):
        try:
            target = parse_date(self.review_date_var.get()).strftime('%Y-%m-%d')
        except ValueError:
            messagebox.showerror('错误', '日期必须是 YYYY-MM-DD。'); return
        if is_before_close_for_today(target):
            messagebox.showwarning('尚未收盘', f'{target} 现在还没到15:05。\n收盘复盘只能取前一交易日及之前数据。')
            prev = latest_completed_local_date(self.dm, True)
            if prev:
                if messagebox.askyesno('切换日期', f'是否改用本地最近一个已完成交易日：{prev}？'):
                    self.review_date_var.set(prev); target = prev
                else:
                    return
            else:
                return
        codes = self.selected_review_codes()
        if not codes:
            messagebox.showwarning('提示', '请至少选择一个下载批次。'); return
        total, q, r = self.dm.date_status(target)
        resume_info = self.dm.get_daily_resume_info(codes, target)
        overwrite = False
        if resume_info['completed'] > 0 and resume_info['completed'] < resume_info['total']:
            choice = messagebox.askyesnocancel(
                '发现未完成下载｜可断点续传',
                f'目标日期：{target}\n已完整保存：{resume_info["completed"]}/{resume_info["total"]} 只\n待继续：{resume_info["pending"]} 只\n\n是：断点续传（推荐）\n否：重新抓取全部所选股票\n取消：不执行'
            )
            if choice is None:
                return
            overwrite = not choice
        elif resume_info['completed'] >= resume_info['total'] and resume_info['total'] > 0:
            if not messagebox.askyesno('数据已完整', f'{target} 的所选股票已经全部存在完整qfq/raw数据。\n是否重新抓取并覆盖？'):
                return
            overwrite = True
        elif target != today_str():
            overwrite = False
        start = (parse_date(target) - dt.timedelta(days=LOAD_CALENDAR_DAYS)).strftime('%Y-%m-%d')
        self.daily_update_running = True
        self.review_progress.configure(maximum=max(1, len(codes)), value=0)
        self.review_progress_label.set(f'准备更新 0/{len(codes)}')
        self.set_status(f'开始更新收盘数据：{target}（可同时使用自选股监控/查看已有数据）')
        threading.Thread(target=self._review_update_worker, args=(codes,start,target,overwrite), daemon=True).start()

    def _review_update_worker(self, codes, start, end, overwrite):
        try:
            def on_progress(done, total, code):
                pct = done / max(1, total) * 100
                self.set_status(f'收盘数据 {done}/{total} - {code}')
                self.ui(self.review_progress.configure, maximum=max(1, total), value=done)
                self.ui(self.review_progress_label.set, f'抓取 {done}/{total}（{pct:.0f}%）｜{code}')

            result = self.dm.update_daily(codes, start, end, overwrite=overwrite,
                                          progress_callback=on_progress,
                                          source=self.data_source_var.get())
            summary = f'日线状态：{end}｜成功 {result["success"]}｜失败 {result["failed"]}｜数据源：{self.data_source_var.get()}'
            self.ui(self.daily_summary_var.set, summary)
            # 收盘数据完成后，轻量检查最近两个已结束季度的股东户数；已有缓存则跳过。
            try:
                sh_result = self.dm.update_shareholder_if_needed(force=False)
                if sh_result.get('enabled'):
                    saved = sh_result.get('saved', 0)
                    skipped = sh_result.get('skipped', 0)
                    if saved:
                        self.set_status(f'收盘数据更新完成；股东户数同步 {saved:,} 条，已缓存跳过 {skipped} 个季度。')
            except Exception as _sh_exc:
                logger.warning(f'收盘后自动同步股东户数失败（忽略）：{_sh_exc}')
            self.ui(self.review_progress.configure, maximum=max(1, result['total']), value=result['total'])
            self.ui(self.review_progress_label.set, f'完成 {result["total"]}/{result["total"]}（100%）')
            fail_count = result.get('failed', 0)
            self.ui(self.set_status, f'收盘数据更新完成：{result["success"]}/{result["total"]}，失败{fail_count}；自选股监控可继续使用。')
            if fail_count:
                preview = '\n'.join([f'{c}: {r}' for c, r in result.get('failures', [])[:8]])
                self.ui(messagebox.showinfo, '日线更新完成（存在失败）', f'成功 {result["success"]} 只，失败 {fail_count} 只。\n\n前8条失败原因：\n{preview}')
            # 日线完成后轻量检查最新已结束季度股东数据；失败不影响主流程。
            try:
                sh_sync = self.dm.update_shareholder_if_needed()
                if sh_sync.get('enabled'):
                    self.set_status(f'收盘数据完成：{result["success"]}/{result["total"]}；股东数据同步完成/跳过。')
            except Exception as sh_exc:
                logger.warning(f'收盘后股东户数同步忽略：{sh_exc}')
            self.ui(self.run_screen)
        except Exception as exc:
            self.ui(self.daily_summary_var.set, f'日线状态：更新失败｜{exc}')
            self.ui(messagebox.showerror, '错误', f'收盘数据更新失败：{exc}')
        finally:
            self.daily_update_running = False

    def run_screen(self):
        if self.screen_running:
            messagebox.showwarning('提示', '筛选任务已经在运行，请稍候。'); return
        try:
            target = parse_date(self.review_date_var.get()).strftime('%Y-%m-%d')
            params = self.read_params()
        except ValueError as exc:
            messagebox.showerror('错误', str(exc)); return
        ds=self.dm.inspect_date(target)
        if ds['raw_count']==0 or ds['qfq_count']==0:
            candidates=[d for d in (ds['prev_date'],ds['next_date']) if d]
            if not candidates:
                messagebox.showwarning('没有可用数据',f'{target} 附近没有可用于复盘的收盘数据。'); return
            target_ts=pd.Timestamp(target); nearest=min(candidates,key=lambda d:abs(pd.Timestamp(d)-target_ts))
            prompt=(f'{target} 没有完整收盘数据。\n\n'
                    f'前一可用交易日：{ds["prev_date"] or "无"}\n'
                    f'后一可用交易日：{ds["next_date"] or "无"}\n\n'
                    f'是否改用最近可用交易日：{nearest}？')
            ok=messagebox.askyesno('所选日期没有完整收盘数据', prompt)
            if not ok: return
            target=nearest; self.review_date_var.set(target)
        else:
            self.set_status(f'{target} 数据可用：QFQ {ds["qfq_count"]} / RAW {ds["raw_count"]}，开始筛选。')
        if is_before_close_for_today(target):
            messagebox.showwarning('尚未收盘', '今天还没到15:05，收盘复盘只能用前一个已完成交易日。'); return

        self.screen_running = True
        self.review_progress['value'] = 0
        self.review_progress_label.set('筛选准备中...')
        self.set_status(f'筛选中：{target}')
        threading.Thread(target=self._screen_worker, args=(params,target), daemon=True).start()

    def _screen_worker(self, params, target):
        try:
            stock_list=self.dm.get_stock_list()
            self.ui(self.review_progress.configure,maximum=max(1,len(stock_list)))
            qfq,raw=self.dm.load_screen_data(target)
            market_ctx=_market_stock_features(raw) if params.get('market_environment_enabled',True) else {'score':50.0,'state':'未启用'}
            self.ui(self.market_env_var.set,f'市场：{market_ctx.get("score",50):.0f}/100｜{market_ctx.get("state","未知")}')
            names=dict(zip(stock_list['code'],stock_list['name']))
            profiles=enrich_profiles_before_screen(self.dm,target,list(qfq.keys()))
            sector_stats=sector_context_map(raw, qfq, profiles)
            results=[]; cap_filtered=0; cap_missing=0; total=len(qfq)
            for idx,code in enumerate(qfq.keys(),1):
                local_params=dict(params); profile=profiles.get(normalize_code(code),{})
                cap=safe_float(profile.get('circulating_market_cap_billion'))
                result=None
                if params.get('market_cap_filter_enabled',False):
                    same_day=(profile.get('snapshot_date')==str(target))
                    if not same_day or cap is None:
                        cap_missing+=1
                    else:
                        lo=float(params.get('min_market_cap',0) or 0); hi=float(params.get('max_market_cap',999999) or 999999)
                        if lo<=cap<=hi:
                            result=screen_stock(code,names.get(code,''),qfq.get(code),raw.get(code),local_params,market_ctx,profile,get_stock_sector_context(code, profiles, sector_stats))
                        else:
                            cap_filtered+=1
                else:
                    result=screen_stock(code,names.get(code,''),qfq.get(code),raw.get(code),local_params,market_ctx,profile,get_stock_sector_context(code, profiles, sector_stats))
                if result: results.append(result)
                if idx==1 or idx%10==0 or idx==total:
                    self.ui(self.review_progress.configure,value=idx)
                    self.ui(self.review_progress_label.set,f'筛选 {idx}/{total}（{idx/max(1,total)*100:.0f}%）')
            df=pd.DataFrame(results)
            if not df.empty:
                df=df.sort_values(['推荐度','严格匹配度','近一年涨停次数'],ascending=[False,False,False]).reset_index(drop=True)
                df=apply_extreme_picks(df, params.get('extreme_pick_count', 5))
                df.insert(0,'排名',range(1,len(df)+1))
            self.result_df=df; self.current_date=target; self.current_mode='收盘复盘'
            self.ui(self.display_review_results)
            if not df.empty: self.dm.save_screen_record(params,df['代码'].tolist(),target,'收盘复盘')
            extra=f'｜市值排除{cap_filtered}只｜同日市值缺失{cap_missing}只' if params.get('market_cap_filter_enabled') else ''
            self.set_status(f'{target} 筛选完成：{len(df)}只｜推荐度优先排序{extra}')
        except Exception as exc:
            self.ui(messagebox.showerror,'错误',f'筛选失败：{exc}')
            self.set_status('筛选失败')
        finally:
            self.screen_running=False
            self.ui(self.review_progress_label.set,'筛选完成')

    def display_review_results(self):
        for i in self.review_tree.get_children(): self.review_tree.delete(i)
        if self.result_df.empty:
            self.review_top5_label.set('极度推荐 TOP 5：无候选')
            self.review_page_label.set('第0/0页')
            self.clear_review_detail()
            return
        total=len(self.result_df)
        top5=self.result_df.sort_values(['极度推荐分','涨停基因分','推荐度'],ascending=[False,False,False],na_position='last').head(5)
        top_items=[]
        for i,(_,r) in enumerate(top5.iterrows(),1):
            top_items.append(f"{i}.{r['代码']} {r['名称']}({float(r.get('极度推荐分',0)):.1f}|{r.get('MA5状态','')}|{r.get('板块状态','')})")
        mode_text='仅显示极度推荐TOP5' if self.review_extreme_only else f'全量{total}只'
        self.review_top5_label.set(f"极度推荐 TOP 5：{'、'.join(top_items)}｜{mode_text}")
        view_df = top5 if self.review_extreme_only else self.result_df
        vtotal=len(view_df)
        pages=max(1,(vtotal+self.review_page_size-1)//self.review_page_size)
        self.review_page=max(1,min(self.review_page,pages))
        start_idx=(self.review_page-1)*self.review_page_size
        page_df=view_df.iloc[start_idx:start_idx+self.review_page_size]
        for _,row in page_df.iterrows():
            self.review_tree.insert('',tk.END,values=[row.get(c,'') for c in self.review_columns])
        self.review_page_label.set(f'第{self.review_page}/{pages}页｜每页{self.review_page_size}只')
        items=self.review_tree.get_children()
        if items:
            self.review_tree.selection_set(items[0]); self.review_tree.focus(items[0]); self.show_review_detail(page_df.iloc[0].to_dict())

    def change_review_page(self, delta):
        if self.result_df.empty: return
        total = 5 if self.review_extreme_only else len(self.result_df)
        pages=max(1,(total+self.review_page_size-1)//self.review_page_size)
        self.review_page=max(1,min(pages,self.review_page+delta)); self.display_review_results()

    def refresh_current_review_chart(self):
        """切换复盘图表指标，不修改筛选结果或数据库。"""
        try:
            sel=self.review_tree.selection()
            if not sel:
                return
            vals=self.review_tree.item(sel[0]).get('values',[])
            if len(vals)<2:
                return
            code=normalize_code(vals[1])
            if code:
                self.plot_stock(code,target_frame=self.review_detail_chart_frame,canvas_attr='review_detail_canvas')
        except Exception as exc:
            logger.exception('刷新复盘图表失败')
            self.set_status(f'图表刷新失败：{exc}')

    def review_select(self,event=None):
        sel=self.review_tree.selection()
        if not sel: return
        values=self.review_tree.item(sel[0])['values']
        if not values: return
        code=normalize_code(values[1])
        rows=self.result_df[self.result_df['代码'].astype(str).map(normalize_code)==code]
        if rows.empty: return
        row=rows.iloc[0].to_dict(); self.show_review_detail(row)
        try: self.plot_stock(code,target_frame=self.review_detail_chart_frame,canvas_attr='review_detail_canvas')
        except Exception as exc: self.set_status(f'K线绘图失败：{exc}')

    def show_review_detail(self,row):
        self.review_detail_text.delete('1.0',tk.END)
        lines=[]
        for title,keylist in [
            ('当前判断',['收盘交易状态','计划依据','关注理由']),
            ('核心指标',['最新价','今日涨跌%','今日状态','极度推荐','极度推荐分','极度推荐理由','推荐度','涨停基因分','涨停基因状态','涨停基因说明','启动评分','基础策略分','严格匹配度','RSI14','KDJ状态','MA5状态','换手率','板块名称','板块状态','板块当日中位涨跌%','板块5日中位涨跌%','行业/主题','流通市值(亿)','参考总市值(亿)','市值状态','市场评分','市场状态','相对强度','相对大盘5日%','MACD零轴','DIF','DEA','MACD柱','近一年涨停次数','量比','5/20量比','20日涨幅%','近10日最大涨幅%']),
            ('激进方案',['激进买点','激进止损','激进止盈','激进周期']),
            ('保守方案',['保守买点','保守止损','保守止盈','保守周期']),
            ('关键位置',['MA5','MA10','MA20','MA60','20日平台高','20日低点','ATR14'])]:
            lines.append(f'【{title}】')
            for k in keylist: lines.append(f'{k}：{row.get(k,"")}')
            lines.append('')
        self.review_detail_text.insert('1.0','\n'.join(lines))

    def clear_review_detail(self):
        if hasattr(self,'review_detail_text'): self.review_detail_text.delete('1.0',tk.END)

    def review_plan_selected(self):
        sel=self.review_tree.selection()
        if not sel:
            messagebox.showinfo('提示','请先选择一只股票。'); return
        values=self.review_tree.item(sel[0])['values']
        code=normalize_code(values[3]) if len(values)>3 else ''
        rows=self.result_df[self.result_df['代码'].astype(str).map(normalize_code)==code]
        if rows.empty: return
        show_close_trade_plan_detail(self.root, rows.iloc[0].to_dict())

    # ---------- 盘中 ----------
    def build_intraday_tab(self):
        top=ttk.LabelFrame(self.tab_intraday,text='② 盘中雷达｜今日采集 + 历史查询 + Top10',padding=8); top.pack(fill=tk.X,padx=8,pady=8)
        r0=ttk.Frame(top); r0.pack(fill=tk.X,pady=3)
        ttk.Label(r0,text='交易日').pack(side=tk.LEFT,padx=4)
        self.intra_date_var=tk.StringVar(value=today_str()); ttk.Entry(r0,textvariable=self.intra_date_var,width=12).pack(side=tk.LEFT,padx=4)
        ttk.Label(r0,text='查询时间(可选)').pack(side=tk.LEFT,padx=4)
        self.intra_query_time_var=tk.StringVar(value=''); ttk.Entry(r0,textvariable=self.intra_query_time_var,width=10).pack(side=tk.LEFT,padx=4)
        ttk.Button(r0,text='采集全市场',command=self.intraday_all_thread).pack(side=tk.LEFT,padx=5)
        ttk.Button(r0,text='采集输入股票',command=self.intraday_watch_thread).pack(side=tk.LEFT,padx=5)
        ttk.Button(r0,text='查询该日采样',command=self.query_intraday_date).pack(side=tk.LEFT,padx=5)
        ttk.Button(r0,text='刷新最近采样',command=self.refresh_intraday_view).pack(side=tk.LEFT,padx=5)
        ttk.Label(r0,text='数据源').pack(side=tk.LEFT,padx=(12,3))
        ttk.Combobox(r0,textvariable=self.data_source_var,values=['auto','baostock','sina','tushare','akshare'],width=10,state='readonly').pack(side=tk.LEFT,padx=3)
        r1=ttk.Frame(top); r1.pack(fill=tk.X,pady=3)
        ttk.Label(r1,text='指定股票').pack(side=tk.LEFT,padx=4); self.intra_codes_var=tk.StringVar(); ttk.Entry(r1,textvariable=self.intra_codes_var,width=45).pack(side=tk.LEFT,padx=4)
        ttk.Label(r1,text='例如：600888、000917').pack(side=tk.LEFT,padx=5)
        ttk.Button(r1,text='查询指定股票',command=self.query_intraday_codes).pack(side=tk.LEFT,padx=5)
        ttk.Button(r1,text='查看抓取日志',command=self.show_intraday_fetch_log).pack(side=tk.LEFT,padx=5)
        self.intra_summary_var=tk.StringVar(value='盘中状态：尚未查询'); ttk.Label(self.tab_intraday,textvariable=self.intra_summary_var,anchor=tk.W).pack(fill=tk.X,padx=12,pady=2)
        self.intra_top5_label=tk.StringVar(value='盘中Top 10：等待采样或查询'); ttk.Label(self.tab_intraday,textvariable=self.intra_top5_label,anchor=tk.W).pack(fill=tk.X,padx=12,pady=2)
        body=ttk.PanedWindow(self.tab_intraday,orient=tk.VERTICAL); body.pack(fill=tk.BOTH,expand=True,padx=8,pady=5)
        table_frame=ttk.LabelFrame(body,text='盘中结果（按盘中评分排序；历史日期也可查询）',padding=5); chart_frame=ttk.LabelFrame(body,text='盘中走势入口：双击股票跳转到自选股监控',padding=5)
        body.add(table_frame,weight=3); body.add(chart_frame,weight=1); self.intra_chart_frame=chart_frame
        cols=('排名','代码','名称','盘中评分','状态','采样时间','最后价','日内涨跌幅%','VWAP','站上VWAP','5分钟动量%','量能加速','数据源','激进入场','激进止损','激进止盈','激进周期','保守入场','保守止损','保守止盈','保守周期','策略依据'); self.intra_cols=cols
        self.intra_tree=ttk.Treeview(table_frame,columns=cols,show='headings')
        for c in cols: self.intra_tree.heading(c,text=c); self.intra_tree.column(c,width=100,anchor=tk.CENTER)
        self.intra_tree.column('策略依据',width=180,anchor=tk.W)
        sy=ttk.Scrollbar(table_frame,orient=tk.VERTICAL,command=self.intra_tree.yview); sx=ttk.Scrollbar(table_frame,orient=tk.HORIZONTAL,command=self.intra_tree.xview); self.intra_tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self.intra_tree.pack(side=tk.TOP,fill=tk.BOTH,expand=True); sy.pack(side=tk.RIGHT,fill=tk.Y); sx.pack(side=tk.BOTTOM,fill=tk.X); self.intra_tree.bind('<Double-1>',self.intraday_tree_double_click)
        self.root.after(120,lambda:self._set_pane_position(body,0.72)); ttk.Label(self.tab_intraday,text='今天可采集；历史日期只能查询已经保存的盘中快照。没有数据时可查看抓取日志。').pack(fill=tk.X,padx=12,pady=4)

    def _check_intraday_ready(self):
        target=parse_date(self.intra_date_var.get()).strftime('%Y-%m-%d')
        if target!=today_str(): raise ValueError('盘中采样只能采今天；历史日期请查询。')
        now=now_time()
        if now<MARKET_OPEN: raise ValueError('还没到09:30，暂时没有盘中行情。')
        if now>=MARKET_CLOSE: raise ValueError('当前已收盘，请使用①收盘复盘获取正式日线。')
        return target

    def intraday_all_thread(self):
        try: target=self._check_intraday_ready()
        except ValueError as exc: messagebox.showwarning('提示',str(exc)); return
        codes=[c for i,b in enumerate(self.dm.get_batches()) if i<len(self.review_batch_vars) and self.review_batch_vars[i].get() for c in b]; self._start_intraday(codes,target)

    def intraday_watch_thread(self):
        try: target=self._check_intraday_ready()
        except ValueError as exc: messagebox.showwarning('提示',str(exc)); return
        codes=parse_codes(self.intra_codes_var.get())
        if not codes: messagebox.showwarning('提示','请输入至少一只股票代码。'); return
        self._start_intraday(codes,target)

    def _start_intraday(self,codes,target):
        threading.Thread(target=self._intraday_worker,args=(codes,target),daemon=True).start(); self.set_status(f'开始盘中采样，共{len(codes)}只；其它模块可继续使用。')

    def _intraday_worker(self,codes,target):
        try:
            result=self.dm.collect_intraday(codes,target,progress_callback=lambda d,t,c:self.set_status(f'盘中采样 {d}/{t} - {c}'),source=self.data_source_var.get()); self.ui(self.refresh_intraday_view)
            used='、'.join(result.get('used_sources',[])) or '无'; self.ui(self.intra_summary_var.set,f'本次采样：成功 {result["success"]}/{result["total"]}｜数据源 {used}｜采样时间 {result["snapshot_time"]}')
            if result.get('errors'):
                self.ui(messagebox.showinfo,'盘中采样结果',f'成功 {result["success"]} 只，异常/无数据 {len(result["errors"])} 只。\n\n'+'\n'.join(result['errors'][:12]))
        except Exception as exc: self.ui(messagebox.showerror,'盘中采样失败',str(exc)); self.set_status('盘中采样失败')

    def _clear_tree(self,tree):
        for i in tree.get_children(): tree.delete(i)

    def _show_intraday_df(self,df,title):
        self._clear_tree(self.intra_tree)
        if df.empty:
            self.intra_summary_var.set(f'{title}：0条｜请先采样或检查日期/时间。'); self.intra_top5_label.set(f'{title}：没有已保存的盘中快照。'); self.intra_latest_df=pd.DataFrame(); return
        df=df.sort_values(['代码','采样时间']).drop_duplicates('代码',keep='last') if '采样时间' in df.columns else df.copy()
        daily_end=latest_completed_local_date(self.dm,before_today=True) or today_str(); qmap,rmap=self.dm.load_screen_data(daily_end); rows=[]
        for _,r in df.iterrows():
            code=normalize_code(r['代码']); bars=self.dm.get_intraday_series(code,r['日期']); prev=self.dm.get_previous_daily_close(code,r['日期']); m=intraday_metrics(bars,prev) if bars is not None and not bars.empty else {}; rec=build_intraday_recommendation(rmap.get(code),qmap.get(code),m,self.params) if m else {}
            rows.append({'代码':code,'名称':r.get('名称',''),'盘中评分':rec.get('盘中综合评分',0),'状态':rec.get('盘中状态','数据不足'),'采样时间':r.get('采样时间',''),'最后价':round(m.get('盘中最新价',r.get('最后价',0)),2) if m else r.get('最后价',''),'日内涨跌幅%':round(m.get('盘中涨跌幅%',r.get('日内涨跌幅',0)),2) if m.get('盘中涨跌幅%') is not None else '','VWAP':round(m['VWAP'],2) if m.get('VWAP') else '','站上VWAP':'是' if m.get('是否站上VWAP') else '否','5分钟动量%':round(float(m['5分钟动量%']),2) if m.get('5分钟动量%') is not None and pd.notna(m.get('5分钟动量%')) else 0.0,'量能加速':round(float(m['5分钟量能加速']),2) if m.get('5分钟量能加速') is not None and pd.notna(m.get('5分钟量能加速')) else 1.0,'数据源':r.get('数据源',''),'激进入场':rec.get('激进入场',''),'激进止损':rec.get('激进止损',''),'激进止盈':rec.get('激进止盈',''),'激进周期':rec.get('激进周期',''),'保守入场':rec.get('保守入场',''),'保守止损':rec.get('保守止损',''),'保守止盈':rec.get('保守止盈',''),'保守周期':rec.get('保守周期',''),'策略依据':rec.get('策略依据','')})
        out=pd.DataFrame(rows).sort_values(['盘中评分','日内涨跌幅%'],ascending=[False,False],na_position='last').reset_index(drop=True); out.insert(0,'排名',range(1,len(out)+1)); self.intra_latest_df=out
        for _,row in out.head(100).iterrows(): self.intra_tree.insert('',tk.END,values=[row.get(c,'') for c in self.intra_cols])
        txt='；'.join([f"{int(row['排名'])}.{row['代码']} {row.get('名称','')}（{row['盘中评分']}分）" for _,row in out.head(10).iterrows()]); self.intra_top5_label.set(f'Top 10：{txt}'); self.intra_summary_var.set(f'{title}：{len(out)}只｜显示前{min(100,len(out))}只')

    def refresh_intraday_view(self):
        df=self.dm.query_intraday_snapshots(today_str()); self._show_intraday_df(df,'今日最近采样')

    def query_intraday_date(self):
        try: target=parse_date(self.intra_date_var.get()).strftime('%Y-%m-%d')
        except ValueError: messagebox.showerror('错误','日期必须是 YYYY-MM-DD。'); return
        st=self.intra_query_time_var.get().strip() or None; df=self.dm.query_intraday_snapshots(target,st)
        if df.empty: messagebox.showinfo('查询结果',f'{target} 没有已保存盘中快照。\n\n如之前尝试过采集，请点击“查看抓取日志”。')
        self._show_intraday_df(df,f'{target}盘中查询'+(f' {st}' if st else ''))

    def query_intraday_codes(self):
        codes=parse_codes(self.intra_codes_var.get())
        if not codes: messagebox.showwarning('提示','请输入股票代码。'); return
        try: target=parse_date(self.intra_date_var.get()).strftime('%Y-%m-%d')
        except ValueError: messagebox.showerror('错误','日期必须是 YYYY-MM-DD。'); return
        st=self.intra_query_time_var.get().strip() or None; df=self.dm.query_intraday_snapshots(target,st,codes=codes)
        if df.empty: messagebox.showinfo('查询结果',f'{target} 没有找到指定股票的已保存盘中快照。'); return
        self._show_intraday_df(df,f'{target} 指定股票')

    def show_intraday_fetch_log(self):
        try: target=parse_date(self.intra_date_var.get()).strftime('%Y-%m-%d')
        except ValueError: messagebox.showerror('错误','日期必须是 YYYY-MM-DD。'); return
        codes=parse_codes(self.intra_codes_var.get()); log=self.dm.query_intraday_fetch_log(target,codes)
        if log.empty: messagebox.showinfo('抓取日志',f'{target} 暂无抓取日志。'); return
        win=tk.Toplevel(self.root); win.title(f'盘中抓取日志｜{target}'); win.geometry('1200x560'); tree=ttk.Treeview(win,columns=tuple(log.columns),show='headings')
        for c in log.columns: tree.heading(c,text=c); tree.column(c,width=160,anchor=tk.CENTER)
        y=ttk.Scrollbar(win,orient=tk.VERTICAL,command=tree.yview); tree.configure(yscrollcommand=y.set); tree.pack(side=tk.LEFT,fill=tk.BOTH,expand=True,padx=(8,0),pady=8); y.pack(side=tk.RIGHT,fill=tk.Y,pady=8)
        for _,r in log.iterrows(): tree.insert('',tk.END,values=list(r.values))

    def show_intraday_detail_popup(self):
        """双击盘中结果，展示完整可读的盘中研究信息，并提供分钟走势入口。"""
        sel = self.intra_tree.selection()
        if not sel:
            messagebox.showinfo('盘中详情', '请先选择一只股票。')
            return

        vals = self.intra_tree.item(sel[0]).get('values', [])
        if not vals:
            messagebox.showwarning('盘中详情', '当前行没有可用数据。')
            return

        row = dict(zip(self.intra_cols, vals))
        code = normalize_code(row.get('代码', ''))
        name = row.get('名称', '')

        win = tk.Toplevel(self.root)
        win.title(f'{code} {name}｜盘中详情')
        win.geometry('820x700')
        win.minsize(700, 560)
        win.transient(self.root)

        outer = ttk.Frame(win, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text=f'{code}｜{name}',
            font=UI_FONT_BOLD
        ).pack(anchor=tk.W)

        ttk.Label(
            outer,
            text=f"采样时间：{row.get('采样时间','暂无')}    数据源：{row.get('数据源','暂无')}",
            font=UI_FONT
        ).pack(anchor=tk.W, pady=(3, 8))

        body = ttk.Frame(outer)
        body.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        content = ttk.Frame(canvas)

        content.bind(
            '<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all'))
        )
        window_id = canvas.create_window((0, 0), window=content, anchor='nw')
        canvas.configure(yscrollcommand=scroll.set)

        def resize_content(event):
            canvas.itemconfigure(window_id, width=event.width)

        canvas.bind('<Configure>', resize_content)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        groups = [
            ('盘中表现', [
                ('最后价', row.get('最后价', '')),
                ('日内涨跌', row.get('日内涨跌幅%', '')),
                ('VWAP', row.get('VWAP', '')),
                ('站上VWAP', row.get('站上VWAP', '')),
                ('5分钟动量', row.get('5分钟动量%', '')),
                ('量能加速', row.get('量能加速', '')),
            ]),
            ('交易参考', [
                ('盘中评分', row.get('盘中评分', '')),
                ('状态', row.get('状态', '')),
                ('激进入场', row.get('激进入场', '')),
                ('激进止损', row.get('激进止损', '')),
                ('激进止盈', row.get('激进止盈', '')),
                ('激进周期', row.get('激进周期', '')),
                ('保守入场', row.get('保守入场', '')),
                ('保守止损', row.get('保守止损', '')),
                ('保守止盈', row.get('保守止盈', '')),
                ('保守周期', row.get('保守周期', '')),
            ]),
            ('判断依据', [
                ('策略依据', row.get('策略依据', '')),
            ])
        ]

        for title, items in groups:
            lf = ttk.LabelFrame(content, text=title, padding=9)
            lf.pack(fill=tk.X, pady=5)
            for key, value in items:
                rf = ttk.Frame(lf)
                rf.pack(fill=tk.X, pady=2)
                ttk.Label(rf, text=f'{key}：', width=13, anchor=tk.E).pack(side=tk.LEFT)
                shown = '暂无数据' if value in ('', None, 'nan', 'NaN') else str(value)
                ttk.Label(
                    rf,
                    text=shown,
                    wraplength=650,
                    justify=tk.LEFT
                ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=(8, 0))

        def open_chart():
            if code:
                try:
                    self.show_intraday_chart(code, self.intra_date_var.get().strip())
                except Exception as exc:
                    messagebox.showerror('盘中走势', f'打开盘中走势失败：{exc}')

        ttk.Button(buttons, text='查看分钟走势', command=open_chart).pack(side=tk.LEFT)
        ttk.Button(buttons, text='关闭', command=win.destroy).pack(side=tk.RIGHT)

    def intraday_tree_double_click(self,event=None):
        self.show_intraday_detail_popup()

    def show_intraday_chart(self, code, trade_date):
        df=self.dm.get_intraday_series(code, trade_date)
        if df is None or df.empty:
            # 尝试从新浪补抓该重点股的5分钟历史数据，并临时展示；全市场不逐只抓。
            try:
                df5=fetch_intraday_from_sina(code, trade_date)
                win=tk.Toplevel(self.root); win.title(f'{code} {trade_date} 5分钟走势'); win.geometry('1100x650')
                tmp=ttk.Frame(win,padding=6); tmp.pack(fill=tk.BOTH,expand=True)
                local=df5.copy(); local['DateTime']=pd.to_datetime(local['日期']+' '+local['时间'],errors='coerce'); local=local.set_index('DateTime')
                local=local.rename(columns={'开盘':'Open','最高':'High','最低':'Low','收盘':'Close','成交量':'Volume'})
                fig,axes=mpf.plot(local[['Open','High','Low','Close','Volume']],type='candle',volume=True,style='yahoo',figsize=(10,5),returnfig=True,tight_layout=True)
                fig.set_dpi(90)
                canvas=FigureCanvasTkAgg(fig,master=tmp); canvas.get_tk_widget().pack(fill=tk.BOTH,expand=True); canvas.draw()
                return
            except Exception as exc:
                messagebox.showinfo('盘中走势',f'{code} 在 {trade_date} 暂无可用5分钟数据。\n\n原因：{exc}')
                return
        win=tk.Toplevel(self.root); win.title(f'{code} {trade_date} 5分钟走势'); win.geometry('1100x650')
        local=df.copy(); local['DateTime']=pd.to_datetime(local['trade_date']+' '+local['bar_time'],errors='coerce'); local=local.set_index('DateTime')
        local=local.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})
        fig,axes=mpf.plot(local[['Open','High','Low','Close','Volume']],type='candle',volume=True,style='yahoo',figsize=(10,5),returnfig=True,tight_layout=True)
        fig.set_dpi(90)
        canvas=FigureCanvasTkAgg(fig,master=win); canvas.get_tk_widget().pack(fill=tk.BOTH,expand=True); canvas.draw()


    # ---------- 自选股 ----------
    def build_watch_tab(self):
        top = ttk.LabelFrame(self.tab_watch, text='特定股票查询 / 监控', padding=10)
        top.pack(fill=tk.X, padx=8, pady=8)

        r0 = ttk.Frame(top); r0.pack(fill=tk.X, pady=3)
        ttk.Label(r0, text='股票代码').pack(side=tk.LEFT, padx=4)
        self.watch_codes_var = tk.StringVar(value='')
        ttk.Entry(r0, textvariable=self.watch_codes_var, width=50).pack(side=tk.LEFT, padx=4)
        ttk.Label(r0, text='支持顿号、逗号、空格，例如 600888、000917').pack(side=tk.LEFT, padx=6)

        r1 = ttk.Frame(top); r1.pack(fill=tk.X, pady=3)
        ttk.Label(r1, text='查询日期').pack(side=tk.LEFT, padx=4)
        self.watch_date_var = tk.StringVar(value=today_str())
        ttk.Entry(r1, textvariable=self.watch_date_var, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Label(r1, text='开始').pack(side=tk.LEFT, padx=3)
        self.watch_start_var = tk.StringVar(value=(dt.date.today()-dt.timedelta(days=60)).strftime('%Y-%m-%d'))
        ttk.Entry(r1, textvariable=self.watch_start_var, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Label(r1, text='结束').pack(side=tk.LEFT, padx=3)
        self.watch_end_var = tk.StringVar(value=today_str())
        ttk.Entry(r1, textvariable=self.watch_end_var, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(r1, text='查询收盘情况', command=self.watch_daily_query).pack(side=tk.LEFT, padx=5)
        ttk.Button(r1, text='查询盘中采样', command=self.watch_intraday_query).pack(side=tk.LEFT, padx=5)
        ttk.Button(r1, text='查询日线历史指标', command=self.watch_daily_history).pack(side=tk.LEFT, padx=5)
        ttk.Button(r1, text='查询盘中历史采样', command=self.watch_intraday_history).pack(side=tk.LEFT, padx=5)
        ttk.Button(r1, text='画选中股票日K', command=self.watch_plot).pack(side=tk.LEFT, padx=5)

        body = ttk.PanedWindow(self.tab_watch, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=5)

        frame = ttk.LabelFrame(body, text='查询结果（可拖动分隔线调整表格/图表比例）', padding=5)
        chart_frame = ttk.LabelFrame(body, text='自选股日K + 成交量 + MACD', padding=5)
        body.add(frame, weight=2)
        body.add(chart_frame, weight=3)
        self.watch_body = body
        self.watch_chart_frame = chart_frame
        watch_chart_bar=ttk.Frame(chart_frame); watch_chart_bar.pack(side=tk.TOP, fill=tk.X, pady=(0,4))
        ttk.Label(watch_chart_bar,text='时间窗口').pack(side=tk.LEFT,padx=3)
        for text,val in [('60日',60),('120日',120),('250日',250),('全部',0)]:
            ttk.Button(watch_chart_bar,text=text,command=lambda v=val:self.set_chart_window(v)).pack(side=tk.LEFT,padx=2)
        ttk.Label(watch_chart_bar,text='鼠标滚轮/工具栏可缩放、平移').pack(side=tk.LEFT,padx=10)
        self.root.after(120, lambda: self._set_pane_position(body, 0.38))

        self.watch_tree = ttk.Treeview(frame, columns=('代码','名称'), show='headings')
        self.watch_tree.heading('代码', text='代码'); self.watch_tree.heading('名称', text='名称')
        self.watch_tree.column('代码', width=80); self.watch_tree.column('名称', width=100)
        y = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.watch_tree.yview)
        x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.watch_tree.xview)
        self.watch_tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.watch_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y.pack(side=tk.RIGHT, fill=tk.Y)
        x.pack(side=tk.BOTTOM, fill=tk.X)
        self.watch_tree.bind('<Double-1>', lambda event: self.watch_plot())

    def get_watch_codes(self):
        codes = parse_codes(self.watch_codes_var.get())
        if not codes: messagebox.showwarning('提示','请输入股票代码。')
        return codes

    def _fill_watch_tree(self, df):
        self.watch_tree['columns'] = tuple(df.columns) if not df.empty else ('代码','名称')
        for i in self.watch_tree.get_children(): self.watch_tree.delete(i)
        for c in self.watch_tree['columns']:
            self.watch_tree.heading(c, text=c); self.watch_tree.column(c, width=105, anchor=tk.CENTER)
        if not df.empty:
            for _, row in df.iterrows(): self.watch_tree.insert('', tk.END, values=list(row.values))

    def watch_daily_query(self):
        codes = self.get_watch_codes()
        if not codes: return
        try: target = parse_date(self.watch_date_var.get()).strftime('%Y-%m-%d')
        except ValueError: messagebox.showerror('错误','日期格式必须是 YYYY-MM-DD。'); return
        df = self.dm.query_watchlist_daily(codes, target)
        self._fill_watch_tree(df)
        self.set_status(f'已查询{target}：{len(df)}只。')

    def watch_intraday_query(self):
        codes = self.get_watch_codes()
        if not codes: return
        target = self.watch_date_var.get().strip()
        try:
            parse_date(target)
        except ValueError:
            messagebox.showerror('错误','日期格式必须是 YYYY-MM-DD。'); return
        df = self.dm.query_watchlist_intraday(codes, target)
        if not df.empty:
            self._fill_watch_tree(df)
            self.set_status(f'盘中采样：{target}，{len(df)}只。')
        else:
            messagebox.showinfo('提示','数据库里还没有这些股票今天的盘中采样。请先到“②盘中采样”执行采集。')

    def watch_daily_history(self):
        codes = self.get_watch_codes()
        if not codes: return
        try:
            start = parse_date(self.watch_start_var.get()).strftime('%Y-%m-%d')
            end = parse_date(self.watch_end_var.get()).strftime('%Y-%m-%d')
        except ValueError:
            messagebox.showerror('错误','开始/结束日期必须为 YYYY-MM-DD。'); return
        if start > end:
            messagebox.showerror('错误','开始日期不能晚于结束日期。'); return
        df = self.dm.query_watchlist_daily_history(codes, start, end)
        self._fill_watch_tree(df)
        self.set_status(f'已查询日线历史：{start}~{end}，{len(df)}条。')

    def watch_intraday_history(self):
        codes = self.get_watch_codes()
        if not codes: return
        try:
            start = parse_date(self.watch_start_var.get()).strftime('%Y-%m-%d')
            end = parse_date(self.watch_end_var.get()).strftime('%Y-%m-%d')
        except ValueError:
            messagebox.showerror('错误','开始/结束日期必须为 YYYY-MM-DD。'); return
        if start > end:
            messagebox.showerror('错误','开始日期不能晚于结束日期。'); return
        df = self.dm.query_watchlist_intraday_history(codes, start, end)
        self._fill_watch_tree(df)
        self.set_status(f'已查询盘中历史采样：{start}~{end}，{len(df)}条。')

    def watch_plot(self):
        codes = self.get_watch_codes()
        if not codes: return
        target = self.watch_date_var.get().strip()
        try: parse_date(target)
        except ValueError:
            messagebox.showerror('错误','日期格式必须是 YYYY-MM-DD。'); return
        self.current_date = target
        self.current_mode = '自选股'
        self.nb.select(self.tab_watch)
        try:
            self.plot_stock(codes[0], target_frame=self.watch_chart_frame, canvas_attr='watch_canvas')
        except Exception as exc:
            self.set_status(f'自选股K线绘图失败：{exc}')
            messagebox.showerror('K线绘图失败', str(exc))

    def set_chart_window(self, days):
        self.chart_days = int(days)
        code = ''
        try:
            if self.current_mode == '收盘复盘' and hasattr(self,'review_tree'):
                sel=self.review_tree.selection()
                if sel:
                    vals=self.review_tree.item(sel[0]).get('values',[])
                    if vals: code=normalize_code(vals[1])
            elif hasattr(self,'watch_codes_var'):
                codes=parse_codes(self.watch_codes_var.get())
                if codes: code=codes[0]
        except Exception:
            code=''
        if code:
            if self.current_mode == '收盘复盘':
                self.plot_stock(code,target_frame=self.review_detail_chart_frame,canvas_attr='review_detail_canvas')
            else:
                self.plot_stock(code,target_frame=self.watch_chart_frame,canvas_attr='watch_canvas')

    # ---------- 图表 ----------
    def plot_stock(self, code, target_frame=None, canvas_attr='review_canvas'):
        """绘制更接近同花顺风格的中文日K：K线+MA5/10/20/60+成交量+MACD。"""
        code = normalize_code(code)
        if target_frame is None:
            target_frame = self.review_chart_frame
            canvas_attr = 'review_canvas'

        df = self.dm.load_chart_data(code, self.current_date, self.chart_days)
        if df.empty:
            messagebox.showinfo('提示', f'{code} 在 {self.current_date} 之前没有足够K线。')
            return

        name = self.dm.get_stock_name(code) or '未知股票'
        chart = df.copy()
        chart['Date'] = pd.to_datetime(chart['trade_date'])
        chart = chart.rename(columns={
            'open': 'Open', 'high': 'High', 'low': 'Low',
            'close': 'Close', 'volume': 'Volume', 'amount': 'Amount'
        })
        chart = chart.set_index('Date')[['Open', 'High', 'Low', 'Close', 'Volume']]

        # === 均线 ===
        ma5 = chart['Close'].rolling(5).mean()
        ma10 = chart['Close'].rolling(10).mean()
        ma20 = chart['Close'].rolling(20).mean()
        ma60 = chart['Close'].rolling(60).mean()
        dif, dea, hist = calculate_macd(pd.DataFrame({'收盘': chart['Close'].values}))

        indicator = getattr(self, 'chart_indicator_var', tk.StringVar(value='MACD')).get()
        addplots = [
            mpf.make_addplot(ma5.values, panel=0, width=0.9),
            mpf.make_addplot(ma10.values, panel=0, width=0.9),
            mpf.make_addplot(ma20.values, panel=0, width=1.15),
            mpf.make_addplot(ma60.values, panel=0, width=1.1),
        ]
        if indicator in ('MACD','ALL'):
            addplots.extend([
                mpf.make_addplot(dif.values, panel=2, width=1.0),
                mpf.make_addplot(dea.values, panel=2, width=1.0),
                mpf.make_addplot(hist.values, panel=2, type='bar', alpha=0.45),
            ])

        if indicator in ('KDJ','ALL'):
            _kdj_df = pd.DataFrame({
                '最高': chart['High'].values,
                '最低': chart['Low'].values,
                '收盘': chart['Close'].values
            })
            _k, _d, _j = calculate_kdj(_kdj_df, 9, 3, 3)
            _p = 2 if indicator == 'KDJ' else 3
            addplots.extend([
                mpf.make_addplot(_k.values, panel=_p, width=1.0),
                mpf.make_addplot(_d.values, panel=_p, width=1.0),
                mpf.make_addplot(_j.values, panel=_p, width=1.0),
            ])

        if indicator in ('RSI','ALL'):
            _rsi = calculate_rsi(pd.DataFrame({'收盘':chart['Close'].values}), 14)
            _p = 2 if indicator == 'RSI' else 4
            addplots.append(mpf.make_addplot(_rsi.values, panel=_p, width=1.0))

        panel_ratios = (4.5, 1.35, 2.15) if indicator != 'ALL' else (4.5, 1.25, 2.0, 2.0, 1.8)


        old_canvas = getattr(self, canvas_attr, None)
        if old_canvas:
            try:
                old_canvas.get_tk_widget().destroy()
            except Exception:
                pass

        marketcolors = mpf.make_marketcolors(
            up='#d63c3c', down='#2e9b57', edge='inherit', wick='inherit', volume='inherit'
        )
        mc_style = mpf.make_mpf_style(
            base_mpf_style='yahoo',
            marketcolors=marketcolors,
            gridstyle='-', gridcolor='#eeeeee', gridaxis='both',
            y_on_right=False, facecolor='#ffffff', figcolor='#ffffff',
            rc={'font.family': 'sans-serif', 'font.sans-serif': [MPL_CHINESE_FONT]}
        )

        fig, axes = mpf.plot(
            chart,
            type='candle',
            volume=True,
            addplot=addplots,
            style=mc_style,
            figsize=(10, 5.6),
            panel_ratios=panel_ratios,
            volume_panel=1,
            main_panel=0,
            returnfig=True,
            tight_layout=False,
            xrotation=0,
            datetime_format='%m-%d',
            show_nontrading=False
        )
        fig.set_dpi(90)
        fig.subplots_adjust(left=0.075, right=0.985, top=0.88, bottom=0.11, hspace=0.08)

        # mplfinance 不同版本 axes 结构可能不同，按 panel 顺序取主轴。
        ax_price = axes[0] if len(axes) >= 1 else None
        ax_volume = axes[2] if len(axes) >= 3 else None
        ax_macd = axes[4] if len(axes) >= 5 else axes[2] if len(axes) >= 3 else axes[-1] if axes else None

        for ax in [ax_price, ax_volume, ax_macd]:
            if ax is not None:
                ax.tick_params(axis='both', labelsize=8)
                ax.grid(True, alpha=0.22, linewidth=0.6)

        if ax_price is not None:
            ax_price.set_ylabel('价格（元）', fontsize=9)
            ax_price.set_title('日K线', loc='left', fontsize=10, pad=7, fontweight='bold')
            ax_price.legend(
                [plt.Line2D([], [], color='#f39c12', lw=1),
                 plt.Line2D([], [], color='#9b59b6', lw=1),
                 plt.Line2D([], [], color='#3498db', lw=1.2),
                 plt.Line2D([], [], color='#27ae60', lw=1.1)],
                ['MA5', 'MA10', 'MA20', 'MA60'],
                loc='upper left', frameon=False, fontsize=8, ncol=4
            )

        if ax_volume is not None:
            ax_volume.set_ylabel('成交量', fontsize=9)
            ax_volume.set_title('成交量', loc='left', fontsize=9, pad=4, fontweight='bold')

        if ax_macd is not None:
            ax_macd.set_ylabel('MACD', fontsize=9)
            ax_macd.set_title('MACD', loc='left', fontsize=9, pad=4, fontweight='bold')
            ax_macd.axhline(0, linewidth=0.8, color='#888888', alpha=0.8)
            ax_macd.legend(
                [plt.Line2D([], [], color='#2980b9', lw=1),
                 plt.Line2D([], [], color='#c0392b', lw=1)],
                ['DIF', 'DEA'], loc='upper left', frameon=False, fontsize=8, ncol=2
            )

        # 中文表头/日期轴：不要让 Matplotlib 自动输出 Apr/May 之类英文月份。
        if ax_macd is not None:
            locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
            ax_macd.xaxis.set_major_locator(locator)
            ax_macd.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
            for label in ax_macd.get_xticklabels():
                label.set_fontsize(8)

        # 所有 panel 的日期轴保持一致的数字日期格式。
        for ax in [ax_price, ax_volume]:
            if ax is not None:
                locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
                ax.xaxis.set_major_locator(locator)
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
                ax.tick_params(axis='x', labelbottom=False)

        # 顶部标题：股票代码｜名称｜日期；彻底放弃默认英文月份。
        fig.suptitle(
            f'{code}｜{name}｜{self.current_date}｜前复权日K',
            fontsize=13, fontweight='bold', y=0.965, fontfamily=MPL_CHINESE_FONT
        )

        # 同花顺式交互：Matplotlib 原生工具栏支持缩放/平移/保存图片。
        old_toolbar = getattr(self, 'review_mpl_toolbar' if canvas_attr == 'review_detail_canvas' else 'watch_mpl_toolbar', None)
        if old_toolbar is not None:
            try: old_toolbar.destroy()
            except Exception: pass
        canvas = FigureCanvasTkAgg(fig, master=target_frame)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        canvas.draw()
        toolbar = NavigationToolbar2Tk(canvas, target_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.BOTTOM, fill=tk.X)
        setattr(self, 'review_mpl_toolbar' if canvas_attr == 'review_detail_canvas' else 'watch_mpl_toolbar', toolbar)
        setattr(self, canvas_attr, canvas)

    # ---------- 历史策略验证：单日 + 日期区间 ----------
    def build_backtest_tab(self):
        top=ttk.LabelFrame(self.tab_backtest,text='⑤ 历史策略验证｜单日精确复刻功能①，或按日期区间批量验证',padding=10)
        top.pack(fill=tk.X,padx=8,pady=8)
        r0=ttk.Frame(top); r0.pack(fill=tk.X,pady=3)
        ttk.Label(r0,text='数据集').pack(side=tk.LEFT,padx=4)
        self.backtest_data_mode_var=tk.StringVar(value='当前主库（只读，精确复刻功能①）')
        self.backtest_data_mode_combo=ttk.Combobox(r0,textvariable=self.backtest_data_mode_var,values=['当前主库（只读，精确复刻功能①）','独立历史库（history_2026.db）'],width=28,state='readonly')
        self.backtest_data_mode_combo.pack(side=tk.LEFT,padx=4)
        self.backtest_data_mode_combo.bind('<<ComboboxSelected>>',lambda e:self._refresh_backtest_db_path())
        ttk.Label(r0,text='数据库').pack(side=tk.LEFT,padx=(8,3))
        self.backtest_db_var=tk.StringVar(value=os.path.abspath('./stock_data.db'))
        ttk.Entry(r0,textvariable=self.backtest_db_var,width=58).pack(side=tk.LEFT,padx=4)
        ttk.Button(r0,text='选择数据库',command=self.choose_backtest_db).pack(side=tk.LEFT,padx=4)
        r1=ttk.Frame(top); r1.pack(fill=tk.X,pady=3)
        ttk.Label(r1,text='验证模式').pack(side=tk.LEFT,padx=4)
        self.backtest_mode_var=tk.StringVar(value='单日验证')
        ttk.Combobox(r1,textvariable=self.backtest_mode_var,values=['单日验证','日期区间批量验证'],width=16,state='readonly').pack(side=tk.LEFT,padx=4)
        ttk.Label(r1,text='验证日期').pack(side=tk.LEFT,padx=(10,3))
        self.backtest_date_var=tk.StringVar(value='2026-08-27'); ttk.Entry(r1,textvariable=self.backtest_date_var,width=12).pack(side=tk.LEFT,padx=3)
        ttk.Label(r1,text='开始日期').pack(side=tk.LEFT,padx=(10,3))
        self.backtest_start_var=tk.StringVar(value='2026-08-01'); ttk.Entry(r1,textvariable=self.backtest_start_var,width=12).pack(side=tk.LEFT,padx=3)
        ttk.Label(r1,text='结束日期').pack(side=tk.LEFT,padx=(8,3))
        self.backtest_end_var=tk.StringVar(value='2026-08-28'); ttk.Entry(r1,textvariable=self.backtest_end_var,width=12).pack(side=tk.LEFT,padx=3)
        ttk.Label(r1,text='推荐Top').pack(side=tk.LEFT,padx=(10,3))
        self.backtest_topn_var=tk.StringVar(value='5'); ttk.Spinbox(r1,from_=1,to=20,textvariable=self.backtest_topn_var,width=5).pack(side=tk.LEFT,padx=3)
        ttk.Label(r1,text='排序口径').pack(side=tk.LEFT,padx=(8,3))
        self.backtest_rank_mode_var=tk.StringVar(value='极度推荐')
        ttk.Combobox(r1,textvariable=self.backtest_rank_mode_var,values=['极度推荐','推荐度'],width=10,state='readonly').pack(side=tk.LEFT,padx=3)
        ttk.Button(r1,text='开始验证',command=self.run_backtest).pack(side=tk.LEFT,padx=8)
        ttk.Button(r1,text='双库一致性校验',command=self.backtest_compare_dbs).pack(side=tk.LEFT,padx=5)
        ttk.Button(r1,text='导出结果',command=self.export_backtest_detail).pack(side=tk.LEFT,padx=3)
        r2=ttk.Frame(top); r2.pack(fill=tk.X,pady=3)
        ttk.Label(r2,text='交易模拟').pack(side=tk.LEFT,padx=4)
        ttk.Label(r2,text='第一止盈').pack(side=tk.LEFT,padx=3)
        self.backtest_trade_target_var=tk.StringVar(value='3.0')
        ttk.Combobox(r2,textvariable=self.backtest_trade_target_var,values=['1.0','2.0','3.0','4.0','5.0'],width=6,state='readonly').pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='%（卖部分）').pack(side=tk.LEFT,padx=2)
        ttk.Label(r2,text='第二止盈').pack(side=tk.LEFT,padx=3)
        self.backtest_trade_target2_var=tk.StringVar(value='6.0')
        ttk.Combobox(r2,textvariable=self.backtest_trade_target2_var,values=['4.0','5.0','6.0','8.0','10.0'],width=6,state='readonly').pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='%（清剩余）').pack(side=tk.LEFT,padx=2)
        ttk.Label(r2,text='软止损').pack(side=tk.LEFT,padx=3)
        self.backtest_trade_soft_stop_var=tk.StringVar(value='-3.0')
        ttk.Entry(r2,textvariable=self.backtest_trade_soft_stop_var,width=6).pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='%（只警戒）').pack(side=tk.LEFT,padx=2)
        ttk.Label(r2,text='硬止损').pack(side=tk.LEFT,padx=3)
        self.backtest_trade_stop_var=tk.StringVar(value='-6.0')
        ttk.Entry(r2,textvariable=self.backtest_trade_stop_var,width=6).pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='%').pack(side=tk.LEFT,padx=2)
        ttk.Label(r2,text='首止盈卖').pack(side=tk.LEFT,padx=3)
        self.backtest_trade_partial_var=tk.StringVar(value='50')
        ttk.Spinbox(r2,from_=10,to=90,increment=10,textvariable=self.backtest_trade_partial_var,width=5).pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='%仓位').pack(side=tk.LEFT,padx=2)
        self.backtest_trade_cost_var=tk.StringVar(value='0.20')
        ttk.Entry(r2,textvariable=self.backtest_trade_cost_var,width=6).pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='%成本').pack(side=tk.LEFT,padx=2)
        ttk.Label(r2,text='T+1开盘买｜最早T+2卖｜冲突日按保守顺序处理').pack(side=tk.LEFT,padx=8)
        ttk.Label(r2,text='交易冷却(天)').pack(side=tk.LEFT,padx=(8,3))
        self.backtest_cooldown_var=tk.StringVar(value='3')
        ttk.Spinbox(r2,from_=0,to=10,textvariable=self.backtest_cooldown_var,width=5).pack(side=tk.LEFT,padx=3)
        ttk.Label(r2,text='0=不冷却').pack(side=tk.LEFT,padx=2)
        self.backtest_progress=ttk.Progressbar(r2,length=360,mode='determinate'); self.backtest_progress.pack(side=tk.LEFT,padx=4)
        self.backtest_progress_label=tk.StringVar(value='就绪'); ttk.Label(r2,textvariable=self.backtest_progress_label).pack(side=tk.LEFT,padx=8)
        self.backtest_summary_var=tk.StringVar(value='单日模式默认使用当前主库，只读｜默认按T+1开盘买、T+2起卖，TP1分批止盈，允许强势继续持有'); ttk.Label(r2,textvariable=self.backtest_summary_var).pack(side=tk.LEFT,padx=10)
        info=ttk.LabelFrame(top,text='重要提示',padding=5); info.pack(fill=tk.X,pady=4)
        self.backtest_info_var=tk.StringVar(value='为了验证功能①，推荐先选“当前主库（只读）”；它与功能①共用同一股票池/历史数据，最容易做到1:1。')
        ttk.Label(info,textvariable=self.backtest_info_var,anchor=tk.W).pack(fill=tk.X)
        body=ttk.PanedWindow(self.tab_backtest,orient=tk.VERTICAL); body.pack(fill=tk.BOTH,expand=True,padx=8,pady=5)
        pick_frame=ttk.LabelFrame(body,text='推荐结果：可选择按极度推荐或推荐度显示Top N；区间模式显示逐日摘要',padding=5)
        result_frame=ttk.LabelFrame(body,text='未来6个交易日验证：只用于验证，不参与推荐',padding=5)
        body.add(pick_frame,weight=3); body.add(result_frame,weight=3)
        pick_cols=('日期','排名','极度推荐排名','极度推荐分','涨停基因分','代码','名称','推荐度','启动评分','基础策略分','严格匹配度','最新价','今日涨跌%','MA5状态','RSI14','KDJ状态','换手率','板块名称','板块状态','激进买点','保守买点','激进止损','保守止损','激进止盈','保守止盈','激进周期','保守周期','市场评分','市场状态','相对强度','行业/主题','流通市值(亿)','参考总市值(亿)','筹码结构','关注理由')
        self.backtest_pick_tree=ttk.Treeview(pick_frame,columns=pick_cols,show='headings')
        pw={'日期':90,'排名':50,'极度推荐排名':90,'极度推荐分':88,'涨停基因分':82,'代码':75,'名称':95,'推荐度':65,'严格匹配度':80,'最新价':70,'收盘交易状态':180,'激进买点':145,'保守买点':145,'激进止损':75,'保守止损':75,'激进止盈':110,'保守止盈':110,'关注理由':280}
        for c in pick_cols: self.backtest_pick_tree.heading(c,text=c); self.backtest_pick_tree.column(c,width=pw.get(c,100),anchor=tk.CENTER)
        self.backtest_pick_tree.column('关注理由',anchor=tk.W)
        pys=ttk.Scrollbar(pick_frame,orient=tk.VERTICAL,command=self.backtest_pick_tree.yview); pxs=ttk.Scrollbar(pick_frame,orient=tk.HORIZONTAL,command=self.backtest_pick_tree.xview)
        self.backtest_pick_tree.configure(yscrollcommand=pys.set,xscrollcommand=pxs.set); self.backtest_pick_tree.pack(fill=tk.BOTH,expand=True); pys.pack(side=tk.RIGHT,fill=tk.Y); pxs.pack(side=tk.BOTTOM,fill=tk.X)
        result_cols=('推荐日','推荐排名','推荐度','代码','名称','T+1开盘买入','买入价','交易结果','持有交易日','卖出价','净收益%','第一止盈%','第二止盈%','软止损%','硬止损%','首止盈卖出仓位%','首次止盈日','最终清仓日','最高浮盈%','最大浮亏%','交易备注','原T+5收盘%')
        self.backtest_result_tree=ttk.Treeview(result_frame,columns=result_cols,show='headings')
        rw={'推荐日':90,'推荐排名':75,'推荐度':65,'代码':75,'名称':95,'T+1开盘买入':105,'买入价':80,'交易结果':105,'持有交易日':80,'卖出价':80,'净收益%':80,'第一止盈%':80,'第二止盈%':80,'软止损%':75,'硬止损%':75,'首止盈卖出仓位%':95,'首次止盈日':90,'最终清仓日':90,'最高浮盈%':90,'最大浮亏%':90,'交易备注':220,'原T+5收盘%':90}
        for c in result_cols: self.backtest_result_tree.heading(c,text=c); self.backtest_result_tree.column(c,width=rw.get(c,90),anchor=tk.CENTER)
        rys=ttk.Scrollbar(result_frame,orient=tk.VERTICAL,command=self.backtest_result_tree.yview); rxs=ttk.Scrollbar(result_frame,orient=tk.HORIZONTAL,command=self.backtest_result_tree.xview)
        self.backtest_result_tree.configure(yscrollcommand=rys.set,xscrollcommand=rxs.set); self.backtest_result_tree.pack(fill=tk.BOTH,expand=True); rys.pack(side=tk.RIGHT,fill=tk.Y); rxs.pack(side=tk.BOTTOM,fill=tk.X)
        self.backtest_pick_df=pd.DataFrame(); self.backtest_result_df=pd.DataFrame(); self.backtest_daily_df=pd.DataFrame(); self.backtest_running=False

    def _refresh_backtest_db_path(self):
        mode=self.backtest_data_mode_var.get()
        if mode.startswith('当前主库'):
            self.backtest_db_var.set(os.path.abspath('./stock_data.db')); self.backtest_info_var.set('当前主库只读：与功能①使用同一 stock_data.db，适合先做1:1复刻验证；本功能不会写入数据库。')
        else:
            self.backtest_db_var.set(os.path.abspath('./history_2026/history_2026.db')); self.backtest_info_var.set('独立历史库：不修改当前 stock_data.db；若股票池/前置历史不完整，结果可能与功能①不同。')

    def choose_backtest_db(self):
        path=filedialog.askopenfilename(title='选择回测数据库',filetypes=[('SQLite数据库','*.db'),('所有文件','*.*')])
        if path: self.backtest_db_var.set(path)

    def _inspect_backtest_db(self,db_path):
        with sqlite3.connect(db_path) as conn:
            stock_count=int(conn.execute('SELECT COUNT(*) FROM stock_list').fetchone()[0]); q_count=int(conn.execute("SELECT COUNT(DISTINCT code) FROM daily_data WHERE adjust_flag='qfq'").fetchone()[0]); r_count=int(conn.execute("SELECT COUNT(DISTINCT code) FROM daily_data WHERE adjust_flag='raw'").fetchone()[0]); min_d=conn.execute('SELECT MIN(trade_date) FROM daily_data').fetchone()[0]; max_d=conn.execute('SELECT MAX(trade_date) FROM daily_data').fetchone()[0]
        return {'stock_count':stock_count,'q_count':q_count,'r_count':r_count,'min_date':min_d,'max_date':max_d}

    def _load_history_for_range(self,db_path,start_date,end_date):
        start_ts=parse_date(start_date); end_ts=parse_date(end_date); buffer_start=(start_ts-dt.timedelta(days=430)).strftime('%Y-%m-%d'); future_end=(end_ts+dt.timedelta(days=20)).strftime('%Y-%m-%d')
        with sqlite3.connect(db_path) as conn:
            stock_df=pd.read_sql_query('SELECT code,name FROM stock_list ORDER BY code',conn)
            q=pd.read_sql_query("SELECT code,trade_date,open,high,low,close,volume,amount,pct_chg FROM daily_data WHERE adjust_flag='qfq' AND trade_date BETWEEN ? AND ? ORDER BY code,trade_date",conn,params=(buffer_start,future_end))
            r=pd.read_sql_query("SELECT code,trade_date,open,high,low,close,volume,amount,pct_chg FROM daily_data WHERE adjust_flag='raw' AND trade_date BETWEEN ? AND ? ORDER BY code,trade_date",conn,params=(buffer_start,future_end))
        names=dict(zip(stock_df['code'].map(normalize_code),stock_df['name']))
        def transform(src):
            out={}
            if src.empty:return out
            src=src.copy(); src['code']=src['code'].map(normalize_code)
            for code,g in src.groupby('code',sort=False):
                df=g.copy(); df['日期']=pd.to_datetime(df['trade_date'],errors='coerce'); df=df.rename(columns={'open':'开盘','high':'最高','low':'最低','close':'收盘','volume':'成交量','amount':'成交额','pct_chg':'涨跌幅'}); df=df[['日期','开盘','最高','最低','收盘','成交量','成交额','涨跌幅']]; out[code]=df.dropna(subset=['日期','收盘']).sort_values('日期').reset_index(drop=True)
            return out
        return names,transform(q),transform(r)

    def backtest_compare_dbs(self):
        try: target=parse_date(self.backtest_date_var.get()).strftime('%Y-%m-%d')
        except Exception: messagebox.showerror('参数错误','验证日期必须是 YYYY-MM-DD。'); return
        hist_path=self.backtest_db_var.get().strip()
        if not os.path.exists(hist_path): messagebox.showwarning('提示','历史数据库不存在。'); return
        try:
            params=self.read_params(); main_df=screen_stocks(self.dm,params,target); hist_dm=DataManager(db_path=hist_path); hist_df=screen_stocks(hist_dm,params,target)
            cmp=self.dm.compare_date_data(hist_path,target); mc=set(main_df['代码']) if not main_df.empty else set(); hc=set(hist_df['代码']) if not hist_df.empty else set(); both=mc&hc; score_diff=[]
            for code in both:
                a=main_df[main_df['代码']==code].iloc[0]; b=hist_df[hist_df['代码']==code].iloc[0]
                if abs(float(a.get('推荐度',0))-float(b.get('推荐度',0)))>0.01 or abs(float(a.get('严格匹配度',0))-float(b.get('严格匹配度',0)))>0.01: score_diff.append(code)
            lines=[f'校验日期：{target}',f'主库推荐：{len(main_df)}只，历史库推荐：{len(hist_df)}只',f'RAW共同股票：{cmp["common"]}，数据字段不一致：{cmp["mismatch"]}',f'共同推荐：{len(both)}，仅主库：{len(mc-hc)}，仅历史库：{len(hc-mc)}',f'共同推荐评分不一致：{len(score_diff)}']
            if mc-hc: lines.append('主库独有：'+'、'.join(sorted(mc-hc)[:30]))
            if hc-mc: lines.append('历史库独有：'+'、'.join(sorted(hc-mc)[:30]))
            if cmp['mismatch']: lines.append('行情差异示例：'+'、'.join(cmp['details']['代码'].head(20).tolist()))
            if score_diff: lines.append('评分差异示例：'+'、'.join(sorted(score_diff)[:20]))
            messagebox.showinfo('双库一致性校验','\n'.join(lines)); self.set_status(f'双库校验完成：{target}｜共同推荐{len(both)}｜行情差异{cmp["mismatch"]}')
        except Exception as exc: messagebox.showerror('双库校验失败',str(exc))

    def run_backtest(self):
        if getattr(self,'backtest_running',False): messagebox.showwarning('提示','历史验证正在运行。'); return
        db_path=self.backtest_db_var.get().strip()
        if not db_path or not os.path.exists(db_path): messagebox.showwarning('提示','请选择有效的回测数据库。'); return
        mode=self.backtest_mode_var.get()
        try:
            topn=int(self.backtest_topn_var.get());
            if not 1<=topn<=20: raise ValueError('推荐Top请填写1~20。')
            if mode=='单日验证': target=parse_date(self.backtest_date_var.get()).strftime('%Y-%m-%d'); start=end=target
            else:
                start=parse_date(self.backtest_start_var.get()).strftime('%Y-%m-%d'); end=parse_date(self.backtest_end_var.get()).strftime('%Y-%m-%d')
                if start>end: raise ValueError('开始日期不能晚于结束日期。')
            params=self.read_params(); meta=self._inspect_backtest_db(db_path)
            trade_target=float(self.backtest_trade_target_var.get()); trade_target2=float(self.backtest_trade_target2_var.get()); soft_stop=float(self.backtest_trade_soft_stop_var.get()); trade_stop=float(self.backtest_trade_stop_var.get()); partial_pct=float(self.backtest_trade_partial_var.get()); trade_cost=float(self.backtest_trade_cost_var.get()); cooldown_days=int(self.backtest_cooldown_var.get())
            if trade_target<=0 or trade_target2<=trade_target or soft_stop>=0 or trade_stop>=soft_stop or trade_cost<0 or cooldown_days<0 or not 0<partial_pct<100: raise ValueError('交易参数需满足：第二止盈>第一止盈>0；软止损<0；硬止损更低；成本>=0；冷却期>=0；首止盈卖出比例10~90%。')
        except Exception as exc: messagebox.showerror('参数错误',str(exc)); return
        rank_mode=self.backtest_rank_mode_var.get().strip() or '极度推荐'
        self.backtest_running=True; self.backtest_params_used=params.copy(); self.backtest_progress.configure(maximum=1,value=0); self.backtest_progress_label.set('正在读取历史数据…'); self.backtest_summary_var.set(f'数据库股票池：{meta["stock_count"]}只｜QFQ：{meta["q_count"]}只｜RAW：{meta["r_count"]}只｜范围：{meta["min_date"]}~{meta["max_date"]}｜排序：{rank_mode}')
        self.backtest_trade_settings=(trade_target,trade_target2,soft_stop,trade_stop,partial_pct,trade_cost,cooldown_days)
        threading.Thread(target=self._backtest_worker,args=(db_path,start,end,topn,mode,rank_mode,params,meta,trade_target,trade_target2,soft_stop,trade_stop,partial_pct,trade_cost,cooldown_days),daemon=True).start()

    def _simulate_trade(self, rdf, signal_day, target_pct, target2_pct,
                        soft_stop_pct, hard_stop_pct, partial_pct, cost_pct):
        """更贴近用户实际：
        T收盘出信号 -> T+1开盘买入；
        T+1只能持有；T+2起动态管理。
        TP1分批止盈，剩余仓位使用“峰值回撤+趋势结构”保护。
        软止损只预警；结构破坏才硬退出。
        日线无法知道同一日的先后顺序，因此冲突时使用保守处理。
        """
        ts = pd.Timestamp(signal_day)
        base = {
            '交易结果':'','持有交易日':'','卖出价':'','净收益%':'',
            '第一止盈%':target_pct,'第二止盈%':target2_pct,
            '软止损%':soft_stop_pct,'硬止损%':hard_stop_pct,
            '首止盈卖出仓位%':partial_pct,'首次止盈日':'','最终清仓日':'',
            '最高浮盈%':'','最大浮亏%':'','交易备注':''
        }
        if rdf is None or rdf.empty:
            base['交易结果']='无数据'; return base

        rdf = rdf.sort_values('日期').reset_index(drop=True).copy()
        future = rdf[rdf['日期'] > ts].head(5).reset_index(drop=True)
        if future.empty:
            base['交易结果']='无T+1'; return base

        buy = safe_float(future.iloc[0]['开盘'])
        if buy is None or buy <= 0:
            base['交易结果']='T+1无有效开盘'; return base

        tp1 = buy * (1 + target_pct/100)
        tp2 = buy * (1 + target2_pct/100)
        soft = buy * (1 + soft_stop_pct/100)
        hard = buy * (1 + hard_stop_pct/100)

        remain = 1.0
        realized = 0.0
        first_done = False
        first_day = ''
        final_day = ''
        peak = buy
        best = -999.0
        worst = 999.0
        notes = []
        frac = max(0.1, min(0.9, partial_pct/100))

        for idx, row in future.iterrows():
            n = idx + 1
            hi = safe_float(row['最高']); lo = safe_float(row['最低']); close = safe_float(row['收盘'])
            day = str(pd.Timestamp(row['日期']).date())
            if hi is not None:
                peak = max(peak, hi)
                best = max(best, (hi/buy-1)*100)
            if lo is not None:
                worst = min(worst, (lo/buy-1)*100)

            # T+1严格持有
            if n == 1:
                if hi is not None and hi >= tp1:
                    notes.append(f'{day}盘中达到第一止盈，但T+1不可卖')
                continue

            # ------- 结构/趋势判断 -------
            recent = rdf[rdf['日期'] <= pd.Timestamp(row['日期'])].tail(20)
            if not recent.empty:
                closes = pd.to_numeric(recent['收盘'], errors='coerce')
                lows = pd.to_numeric(recent['最低'], errors='coerce')
                ma5 = closes.tail(5).mean()
                ma10 = closes.tail(10).mean()
                ma20 = closes.tail(20).mean()
                prev5_low = lows.iloc[:-1].tail(5).min() if len(lows) >= 2 else lows.min()
                structure_break = close is not None and (
                    (pd.notna(ma20) and close < ma20 and close < prev5_low)
                )
            else:
                ma5 = ma10 = ma20 = np.nan
                structure_break = False

            # ------- 第一止盈：达到 +3%左右，先卖部分 -------
            if remain > 0 and not first_done and hi is not None and hi >= tp1:
                q1 = min(frac, remain)
                realized += q1 * target_pct
                remain -= q1
                first_done = True
                first_day = f'T+{n}'
                notes.append(f'{day}达到第一止盈：卖出{partial_pct:.0f}%')

            # ------- 已有利润后：移动止盈保护 -------
            if first_done and remain > 0 and peak > buy:
                peak_gain = (peak / buy - 1) * 100
                if peak_gain >= target_pct:
                    # 随盈利增加，给更宽的回撤空间，让强趋势继续跑
                    trail = 2.2 if peak_gain < 5 else (2.8 if peak_gain < 8 else 3.5)
                    trail_price = peak * (1 - trail/100)
                    if close is not None and close <= trail_price:
                        realized += remain * ((close/buy-1)*100)
                        final_day = f'T+{n}'
                        return {
                            **base,'交易结果':'移动止盈退出','持有交易日':n,
                            '卖出价':round(close,2),'净收益%':round(realized-cost_pct,2),
                            '首次止盈日':first_day,'最终清仓日':final_day,
                            '最高浮盈%':round(best,2),'最大浮亏%':round(worst,2),
                            '交易备注':'；'.join(notes+[f'{day}从峰值回撤约{trail:.1f}%后退出'])
                        }

            # ------- 第二止盈/涨停级别：强势直接清仓 -------
            if remain > 0 and hi is not None:
                day_gain = (hi/buy-1)*100
                if day_gain >= target2_pct or day_gain >= 9.8:
                    realized += remain * target2_pct
                    final_day=f'T+{n}'
                    note='达到第二止盈'
                    if day_gain >= 9.8:
                        note='盘中达到涨停级别，剩余仓位清仓'
                    return {
                        **base,'交易结果':'强势止盈/清仓','持有交易日':n,
                        '卖出价':round(tp2 if day_gain < 9.8 else buy*1.098,2),
                        '净收益%':round(realized-cost_pct,2),
                        '首次止盈日':first_day,'最终清仓日':final_day,
                        '最高浮盈%':round(best,2),'最大浮亏%':round(worst,2),
                        '交易备注':'；'.join(notes+[note])
                    }

            # ------- 软止损：仅警报，不立即卖 -------
            if lo is not None and lo <= soft:
                notes.append(f'{day}触发软止损预警')

            # ------- 硬止损：结构破坏 + 收盘确认 -------
            if remain > 0 and close is not None and close <= hard and structure_break:
                realized += remain * ((close/buy-1)*100)
                final_day=f'T+{n}'
                return {
                    **base,'交易结果':'结构止损退出','持有交易日':n,
                    '卖出价':round(close,2),'净收益%':round(realized-cost_pct,2),
                    '首次止盈日':first_day,'最终清仓日':final_day,
                    '最高浮盈%':round(best,2),'最大浮亏%':round(worst,2),
                    '交易备注':'；'.join(notes+[f'{day}收盘确认结构破坏'])
                }

        # T+6结束，剩余仓位按收盘退出
        last = future.iloc[-1]; close = safe_float(last['收盘'])
        if close is None:
            base['交易结果']='无法退出'; return base
        if remain > 0:
            realized += remain*((close/buy-1)*100)
            remain = 0.0
        final_day=f'T+{len(future)}'
        result='首止盈后T+6退出' if first_done else 'T+6收盘退出'
        if not notes: notes.append('5日内未触发关键退出信号')
        return {
            **base,'交易结果':result,'持有交易日':len(future),
            '卖出价':round(close,2),'净收益%':round(realized-cost_pct,2),
            '首次止盈日':first_day,'最终清仓日':final_day,
            '最高浮盈%':round(best,2),'最大浮亏%':round(worst,2),
            '交易备注':'；'.join(notes)
        }

    def _future_metrics(self,rdf,day,record):
        ts=pd.Timestamp(day); base=rdf[rdf['日期']==ts]
        empty={
            '推荐日收盘':'','T+1涨跌%':'','T+2累计%':'','T+3累计%':'','T+4累计%':'','T+5累计%':'','T+6累计%':'',
            '5日最高涨幅%':'','5日最低涨幅%':'','6日最高涨幅%':'','6日最低涨幅%':'',
            '首次≥3%':'','首次≥5%':'','首次≥8%':'','6日内涨停':'','6日涨停次数':'','6日内跌停':'','6日跌停次数':'',
            '买入后6日最高涨幅%':'','买入后6日最低涨幅%':'','买入后首次≥8%':'','6日结果':''
        }
        if base.empty:
            record.update(empty); record['6日结果']='缺少推荐日收盘'; return record
        b=safe_float(base.iloc[-1]['收盘'])
        future=rdf[rdf['日期']>ts].sort_values('日期').head(6).reset_index(drop=True)
        record['推荐日收盘']=round(b,2) if b is not None else ''
        vals={}
        if b:
            for n in range(1,7):
                if len(future)>=n:
                    c=safe_float(future.iloc[n-1]['收盘'])
                    if c is not None: vals[n]=(c/b-1)*100
        for n in range(1,7):
            key='T+1涨跌%' if n==1 else f'T+{n}累计%'
            record[key]=round(vals[n],2) if n in vals else ''
        if not future.empty and b:
            hp=(pd.to_numeric(future['最高'],errors='coerce')/b-1)*100
            lp=(pd.to_numeric(future['最低'],errors='coerce')/b-1)*100
            record['5日最高涨幅%']=round(float(hp.head(5).max()),2) if not hp.head(5).dropna().empty else ''
            record['5日最低涨幅%']=round(float(lp.head(5).min()),2) if not lp.head(5).dropna().empty else ''
            record['6日最高涨幅%']=round(float(hp.max()),2) if not hp.dropna().empty else ''
            record['6日最低涨幅%']=round(float(lp.min()),2) if not lp.dropna().empty else ''

            for threshold in (3,5,8):
                hit=''
                for n in range(1,7):
                    hv=hp.iloc[:n].max()
                    if pd.notna(hv) and hv >= threshold:
                        hit=f'T+{n}'; break
                record[f'首次≥{threshold}%']=hit

            pct_s=pd.to_numeric(future['涨跌幅'],errors='coerce') if '涨跌幅' in future.columns else pd.Series(dtype=float)
            if len(pct_s)==len(future):
                ups=pct_s>=9.8; downs=pct_s<=-9.8
                record['6日内涨停']='是' if bool(ups.any()) else '否'
                record['6日涨停次数']=int(ups.sum())
                record['6日内跌停']='是' if bool(downs.any()) else '否'
                record['6日跌停次数']=int(downs.sum())

            buy=safe_float(future.iloc[0].get('开盘'))
            if buy and buy>0:
                bhp=(pd.to_numeric(future['最高'],errors='coerce')/buy-1)*100
                blp=(pd.to_numeric(future['最低'],errors='coerce')/buy-1)*100
                record['买入后6日最高涨幅%']=round(float(bhp.max()),2) if not bhp.dropna().empty else ''
                record['买入后6日最低涨幅%']=round(float(blp.min()),2) if not blp.dropna().empty else ''
                hit8=''
                for n in range(1,7):
                    hv=bhp.iloc[:n].max()
                    if pd.notna(hv) and hv>=8:
                        hit8=f'T+{n}'; break
                record['买入后首次≥8%']=hit8

        record['6日结果']='完整6日' if len(future)>=6 else (f'仅有{len(future)}个后续交易日' if len(future) else '暂无后续数据')
        return record

    def _backtest_worker(self,db_path,start,end,topn,mode,rank_mode,params,meta,trade_target,trade_target2,soft_stop,trade_stop,partial_pct,trade_cost,cooldown_days):
        try:
            names,qmap,rmap=self._load_history_for_range(db_path,start,end); all_codes=sorted(set(qmap.keys())&set(rmap.keys())); dates=sorted(set(pd.to_datetime(d).strftime('%Y-%m-%d') for df in rmap.values() for d in df.loc[df['日期'].between(pd.Timestamp(start),pd.Timestamp(end)),'日期']))
            if not dates: raise RuntimeError('指定日期范围内没有可用交易日数据。')
            pick_rows=[]; result_rows=[]; daily_rows=[]
            last_signal_index={}
            for di,day in enumerate(dates,1):
                candidates=[]; ts=pd.Timestamp(day)
                backtest_raw={c:rmap[c][rmap[c]['日期']<=ts] for c in all_codes}
                backtest_market_ctx=_market_stock_features(backtest_raw) if params.get('market_environment_enabled',True) else {'score':50.0,'state':'未启用'}
                sh_map = {}
                try:
                    with sqlite3.connect(db_path) as sh_conn:
                        if table_exists_sqlite(sh_conn, 'shareholder_data'):
                            shdf = pd.read_sql_query('''
                                SELECT code, report_date, announce_date, holder_count, prev_holder_count,
                                       holder_change, holder_change_pct, avg_hold_value, avg_hold_quantity
                                FROM shareholder_data
                                WHERE (announce_date IS NULL OR announce_date<=?)
                                  AND report_date<=?
                                ORDER BY code, report_date DESC, announce_date DESC
                            ''', sh_conn, params=(day, day))
                            if not shdf.empty:
                                shdf['code']=shdf['code'].map(normalize_code)
                                shdf=shdf.drop_duplicates('code', keep='first')
                                sh_map={r['code']:r.to_dict() for _,r in shdf.iterrows()}
                except Exception:
                    sh_map = {}
                for idx,code in enumerate(all_codes,1):
                    q_until=qmap[code][qmap[code]['日期']<=ts]; r_until=rmap[code][rmap[code]['日期']<=ts]
                    params['_shareholder_record'] = sh_map.get(code)
                    result=screen_stock(code,names.get(code,''),q_until,r_until,dict(params),backtest_market_ctx)
                    if result: candidates.append(result)
                    if mode=='单日验证' and (idx==1 or idx%50==0 or idx==len(all_codes)):
                        pct=idx/max(1,len(all_codes))*100; self.ui(self.backtest_progress.configure,value=pct); self.ui(self.backtest_progress_label.set,f'复刻功能①筛选 {idx}/{len(all_codes)}（{pct:.0f}%）｜{day}')
                cdf=pd.DataFrame(candidates)
                if cdf.empty:
                    daily_rows.append({'日期':day,'候选数':0,'原始候选数':0,'冷却跳过':0,'TopN':0,'T+1上涨率':'','T+2上涨率':'','T+3上涨率':'','T+4上涨率':'','T+5上涨率':'','T+1平均收益%':'','T+2平均收益%':'','T+3平均收益%':'','T+4平均收益%':'','T+5平均收益%':'','T+5涨停率':'','状态':'无候选'}); continue
                original_candidate_count=len(cdf)
                if cooldown_days>0:
                    keep=[]; skipped=0
                    for _,row in cdf.iterrows():
                        code0=normalize_code(row.get('代码',''))
                        last=last_signal_index.get(code0)
                        if last is not None and di-last<=cooldown_days:
                            skipped+=1
                        else:
                            keep.append(row)
                    cdf=pd.DataFrame(keep)
                else:
                    skipped=0
                if cdf.empty:
                    daily_rows.append({'日期':day,'候选数':0,'原始候选数':original_candidate_count,'冷却跳过':skipped,'TopN':0,'T+1上涨率':'','T+2上涨率':'','T+3上涨率':'','T+4上涨率':'','T+5上涨率':'','T+1平均收益%':'','T+2平均收益%':'','T+3平均收益%':'','T+4平均收益%':'','T+5平均收益%':'','T+5涨停率':'','状态':'全部被冷却'}); continue
                cdf=apply_extreme_picks(cdf, params.get('extreme_pick_count', 5))
                if rank_mode=='极度推荐':
                    cdf=cdf.sort_values(['极度推荐分','涨停基因分','推荐度'],ascending=[False,False,False],na_position='last').reset_index(drop=True)
                else:
                    cdf=cdf.sort_values(['推荐度','启动评分','严格匹配度','近一年涨停次数'],ascending=[False,False,False,False],na_position='last').reset_index(drop=True)
                cdf.insert(0,'排名',range(1,len(cdf)+1)); picks=cdf.head(topn)
                for _,rpick in picks.iterrows(): last_signal_index[normalize_code(rpick.get('代码',''))]=di
                for _,rec in picks.iterrows():
                    code=normalize_code(rec['代码']); trade=self._simulate_trade(rmap.get(code,pd.DataFrame()),day,trade_target,trade_target2,soft_stop,trade_stop,partial_pct,trade_cost); pick_rows.append({'日期':day,'排名':rec['排名'],'极度推荐排名':rec.get('极度推荐排名',''),'极度推荐分':rec.get('极度推荐分',''),'涨停基因分':rec.get('涨停基因分',''),'代码':code,'名称':rec.get('名称',''),'推荐度':rec.get('推荐度',''),'严格匹配度':rec.get('严格匹配度',''),'最新价':rec.get('最新价',''),'今日涨跌%':rec.get('今日涨跌%',''),'MA5状态':rec.get('MA5状态',''),'RSI14':rec.get('RSI14',''),'KDJ状态':rec.get('KDJ状态',''),'换手率':rec.get('换手率',''),'板块名称':rec.get('板块名称',''),'板块状态':rec.get('板块状态',''),'筹码结构':rec.get('筹码结构',''),'行业/主题':rec.get('行业/主题',''),'流通市值(亿)':rec.get('流通市值(亿)',''),'参考总市值(亿)':rec.get('参考总市值(亿)',''),'收盘交易状态':rec.get('收盘交易状态',''),'激进买点':rec.get('激进买点',''),'保守买点':rec.get('保守买点',''),'激进止损':rec.get('激进止损',''),'保守止损':rec.get('保守止损',''),'激进止盈':rec.get('激进止盈',''),'保守止盈':rec.get('保守止盈',''),'关注理由':rec.get('关注理由','')}); base=self._future_metrics(rmap.get(code,pd.DataFrame()),day,{'推荐日':day,'推荐排名':rec['排名'],'极度推荐排名':rec.get('极度推荐排名',''),'极度推荐分':rec.get('极度推荐分',''),'涨停基因分':rec.get('涨停基因分',''),'推荐度':rec.get('推荐度',''),'代码':code,'名称':rec.get('名称','')});
                    base.update({'T+1开盘买入': '是' if trade.get('交易结果') not in ('无数据','无T+1') else '否','买入价': (round(safe_float(rmap.get(code).loc[rmap.get(code)['日期']>pd.Timestamp(day)].iloc[0]['开盘']),2) if code in rmap and not rmap.get(code).empty and not rmap.get(code).loc[rmap.get(code)['日期']>pd.Timestamp(day)].empty and safe_float(rmap.get(code).loc[rmap.get(code)['日期']>pd.Timestamp(day)].iloc[0]['开盘']) is not None else '' ) if code in rmap else '', **trade,'原T+5收盘%': base.get('T+5累计%','')}); result_rows.append(base)
                if mode=='日期区间批量验证':
                    latest=pd.DataFrame(result_rows).tail(len(picks)); vals={n:[] for n in range(1,6)}; highs=[]
                    for _,rr in latest.iterrows():
                        for n in range(1,6):
                            key='T+1涨跌%' if n==1 else f'T+{n}累计%'; v=safe_float(rr.get(key));
                            if v is not None: vals[n].append(v)
                        h=safe_float(rr.get('5日最高涨幅%')); highs.append(h) if h is not None else None
                    avg=lambda a: round(float(np.mean(a)),2) if a else ''; rate=lambda a: round(float(np.mean([v>0 for v in a]))*100,1) if a else ''
                    daily_rows.append({'日期':day,'候选数':len(cdf),'原始候选数':original_candidate_count,'冷却跳过':skipped,'TopN':len(picks),'T+1上涨率':rate(vals[1]),'T+2上涨率':rate(vals[2]),'T+3上涨率':rate(vals[3]),'T+4上涨率':rate(vals[4]),'T+5上涨率':rate(vals[5]),'T+1平均收益%':avg(vals[1]),'T+2平均收益%':avg(vals[2]),'T+3平均收益%':avg(vals[3]),'T+4平均收益%':avg(vals[4]),'T+5平均收益%':avg(vals[5]),'T+5涨停率':round(sum(v>=9.8 for v in highs)/len(highs)*100,1) if highs else '','状态':'有效'})
                if mode=='日期区间批量验证':
                    pct=di/max(1,len(dates))*100; self.ui(self.backtest_progress.configure,maximum=len(dates),value=di); self.ui(self.backtest_progress_label.set,f'批量验证 {di}/{len(dates)}（{pct:.0f}%）｜{day}｜{rank_mode}')
            self.ui(self.backtest_progress.configure,maximum=max(1,len(all_codes) if mode=='单日验证' else len(dates)),value=max(1,len(all_codes) if mode=='单日验证' else len(dates)))
            self.backtest_pick_df=pd.DataFrame(pick_rows); self.backtest_result_df=pd.DataFrame(result_rows); self.backtest_daily_df=pd.DataFrame(daily_rows); self.ui(self._fill_backtest_trees)
            trade_df=pd.DataFrame(result_rows)
            if not trade_df.empty and '净收益%' in trade_df.columns:
                wins=int((pd.to_numeric(trade_df['净收益%'],errors='coerce')>0).sum()); total_tr=len(trade_df); avg_net=float(pd.to_numeric(trade_df['净收益%'],errors='coerce').dropna().mean()) if pd.to_numeric(trade_df['净收益%'],errors='coerce').notna().any() else 0
                trade_summary=f'交易模拟：TP1 {trade_target:.1f}%/{partial_pct:.0f}%仓｜TP2 {trade_target2:.1f}%｜软止损{soft_stop:.1f}%｜硬止损{trade_stop:.1f}%｜成本{trade_cost:.2f}%｜冷却{cooldown_days}天｜盈利率{wins/max(1,total_tr)*100:.1f}%｜平均净收益{avg_net:.2f}%'
            else: trade_summary='交易模拟：暂无有效交易样本'
            if mode=='单日验证': self.ui(self.backtest_summary_var.set,f'{start}：候选{len(candidates)}只｜{rank_mode} Top{topn}｜数据库股票池{meta["stock_count"]}只｜{trade_summary}'); self.set_status(f'历史验证完成：{start}；排序口径：{rank_mode}。')
            else: self.ui(self.backtest_summary_var.set,f'区间完成：{start}~{end}｜交易日{len(dates)}｜推荐明细{len(result_rows)}条'); self.set_status(f'历史区间验证完成：{start}~{end}。')
        except Exception as exc:
            self.ui(messagebox.showerror,'历史验证失败',str(exc)); self.set_status('历史验证失败')
        finally: self.backtest_running=False

    def _fill_backtest_trees(self):
        for tree in (self.backtest_pick_tree,self.backtest_result_tree):
            for item in tree.get_children(): tree.delete(item)
        for _,row in self.backtest_pick_df.iterrows(): self.backtest_pick_tree.insert('',tk.END,values=[row.get(c,'') for c in self.backtest_pick_tree['columns']])
        for _,row in self.backtest_result_df.iterrows(): self.backtest_result_tree.insert('',tk.END,values=[row.get(c,'') for c in self.backtest_result_tree['columns']])

    def export_backtest_detail(self):
        if self.backtest_result_df is None or self.backtest_result_df.empty: messagebox.showwarning('提示','没有历史验证结果。'); return
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='backtest_result.csv',filetypes=[('CSV文件','*.csv')])
        if path: self.backtest_result_df.to_csv(path,index=False,encoding='utf-8-sig'); messagebox.showinfo('成功',f'已导出：\n{path}')

    # ---------- 推荐股跟踪 ----------
    def build_tracking_tab(self):
        top = ttk.LabelFrame(self.tab_tracking, text='④ 推荐股跟踪：横向看最近N个交易日表现 + 原始明细', padding=10)
        top.pack(fill=tk.X, padx=8, pady=8)
        r0 = ttk.Frame(top); r0.pack(fill=tk.X, pady=3)
        ttk.Label(r0, text='推荐CSV').pack(side=tk.LEFT, padx=4)
        self.track_file_var = tk.StringVar()
        ttk.Entry(r0, textvariable=self.track_file_var, width=72).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='选择CSV', command=self.import_tracking_csv).pack(side=tk.LEFT, padx=5)
        ttk.Button(r0, text='开始整理/跟踪', command=self.run_tracking).pack(side=tk.LEFT, padx=5)
        ttk.Label(r0, text='展示最近').pack(side=tk.LEFT, padx=(14,3))
        self.track_days_var = tk.StringVar(value='5')
        ttk.Spinbox(r0, from_=1, to=30, textvariable=self.track_days_var, width=5).pack(side=tk.LEFT, padx=3)
        ttk.Label(r0, text='个交易日').pack(side=tk.LEFT, padx=3)
        ttk.Button(r0, text='导出汇总CSV', command=self.export_tracking_summary_csv).pack(side=tk.LEFT, padx=8)
        ttk.Button(r0, text='导出原始明细CSV', command=self.export_tracking_detail_csv).pack(side=tk.LEFT, padx=3)
        self.track_summary_var = tk.StringVar(value='尚未导入推荐CSV')
        ttk.Label(top, textvariable=self.track_summary_var).pack(anchor=tk.W, padx=4, pady=3)

        body = ttk.PanedWindow(self.tab_tracking, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=5)
        sf = ttk.LabelFrame(body, text='整理后的横向汇总：一只股票一行；日期列动态取推荐日之后最近N个交易日', padding=5)
        df = ttk.LabelFrame(body, text='原始逐日明细：保留数据库中该推荐股后续每个交易日记录', padding=5)
        body.add(sf, weight=3); body.add(df, weight=3)

        self.track_summary_tree = ttk.Treeview(sf, columns=('代码','名称','推荐度','推荐排名','推荐日','推荐日收盘'), show='headings')
        for c in self.track_summary_tree['columns']:
            self.track_summary_tree.heading(c, text=c); self.track_summary_tree.column(c, width=100, anchor=tk.CENTER)
        sy = ttk.Scrollbar(sf, orient=tk.VERTICAL, command=self.track_summary_tree.yview)
        sx = ttk.Scrollbar(sf, orient=tk.HORIZONTAL, command=self.track_summary_tree.xview)
        self.track_summary_tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.track_summary_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y); sx.pack(side=tk.BOTTOM, fill=tk.X)

        self.track_detail_tree = ttk.Treeview(df, columns=('推荐排名','推荐度','代码','名称','推荐日','交易日','T+N','收盘','当日涨跌%','累计涨幅%','数据状态'), show='headings')
        dw={'推荐排名':70,'推荐度':70,'代码':75,'名称':95,'推荐日':90,'交易日':90,'T+N':55,'收盘':75,'当日涨跌%':85,'累计涨幅%':90,'数据状态':180}
        for c in self.track_detail_tree['columns']:
            self.track_detail_tree.heading(c,text=c); self.track_detail_tree.column(c,width=dw.get(c,90),anchor=tk.CENTER)
        yd=ttk.Scrollbar(df,orient=tk.VERTICAL,command=self.track_detail_tree.yview)
        xd=ttk.Scrollbar(df,orient=tk.HORIZONTAL,command=self.track_detail_tree.xview)
        self.track_detail_tree.configure(yscrollcommand=yd.set,xscrollcommand=xd.set)
        self.track_detail_tree.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); yd.pack(side=tk.RIGHT,fill=tk.Y); xd.pack(side=tk.BOTTOM,fill=tk.X)
        self.tracking_result_df = pd.DataFrame()
        self.tracking_summary_df = pd.DataFrame()

    def import_tracking_csv(self):
        path = filedialog.askopenfilename(filetypes=[('CSV文件','*.csv')])
        if not path: return
        self.track_file_var.set(path)
        m = re.search(r'screen_(\d{4}-\d{2}-\d{2})', os.path.basename(path), re.I)
        self.track_summary_var.set(f'已识别推荐日期：{m.group(1)}｜点击“开始整理/跟踪”' if m else '未从文件名识别日期：建议文件名为 screen_YYYY-MM-DD.csv')

    def run_tracking(self):
        path = self.track_file_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning('提示','请选择有效的推荐CSV。'); return
        try:
            recent_n = int(self.track_days_var.get())
            if recent_n < 1 or recent_n > 30:
                raise ValueError('最近交易日数量请填写 1~30。')
            m = re.search(r'screen_(\d{4}-\d{2}-\d{2})', os.path.basename(path), re.I)
            rec_date = m.group(1) if m else ''
            src = pd.read_csv(path, encoding='utf-8-sig', dtype={'代码':str})
            if '代码' not in src.columns: raise ValueError('CSV中没有“代码”列。')
            if not rec_date and '日期' in src.columns: rec_date = str(src.iloc[0]['日期'])[:10]
            if not rec_date: raise ValueError('无法识别推荐日期，请使用 screen_YYYY-MM-DD.csv。')

            codes = list(dict.fromkeys(normalize_code(x) for x in src['代码'].tolist()))
            codes = [c for c in codes if c]
            if not codes: raise ValueError('CSV中没有有效股票代码。')
            name_map = dict(zip(codes, src['名称'].astype(str).tolist())) if '名称' in src.columns else {}
            rank_map = dict(zip(codes,src['排名'].tolist())) if '排名' in src.columns else {}
            score_col='严格匹配度' if '严格匹配度' in src.columns else ('推荐度' if '推荐度' in src.columns else '')
            score_map=dict(zip(codes,src[score_col].tolist())) if score_col else {}
            qmarks=','.join('?'*len(codes))
            with sqlite3.connect(self.dm.db_path) as conn:
                base=pd.read_sql_query(
                    f"SELECT code,trade_date,close FROM daily_data WHERE adjust_flag='raw' AND code IN ({qmarks}) AND trade_date<=? ORDER BY code,trade_date",
                    conn,params=codes+[rec_date])
                fut=pd.read_sql_query(
                    f"SELECT code,trade_date,open,high,low,close,volume,amount,pct_chg FROM daily_data WHERE adjust_flag='raw' AND code IN ({qmarks}) AND trade_date>? ORDER BY trade_date,code",
                    conn,params=codes+[rec_date])
            if not base.empty: base['code']=base['code'].map(normalize_code)
            if not fut.empty:
                fut['code']=fut['code'].map(normalize_code)
                fut['trade_date']=pd.to_datetime(fut['trade_date']).dt.strftime('%Y-%m-%d')

            base_map={}
            for code in codes:
                b=base[base.code==code] if not base.empty else pd.DataFrame()
                if not b.empty:
                    exact=b[b['trade_date'].astype(str)==rec_date]
                    rr=exact.iloc[-1] if not exact.empty else b.iloc[-1]
                    base_map[code]=float(rr['close']) if pd.notna(rr['close']) else None
                else: base_map[code]=None

            available_dates=sorted(pd.Series(fut['trade_date'].dropna().unique()).astype(str).tolist()) if not fut.empty else []
            selected_dates=available_dates[-recent_n:] if len(available_dates)>recent_n else available_dates
            detail=[]; summary=[]
            for code in codes:
                bclose=base_map.get(code)
                d=fut[fut.code==code].copy() if not fut.empty else pd.DataFrame()
                if not d.empty and selected_dates: d=d[d['trade_date'].isin(selected_dates)].sort_values('trade_date')
                row={'代码':code,'名称':name_map.get(code,''),'推荐度':score_map.get(code,''),'推荐排名':rank_map.get(code,''),'推荐日':rec_date,'推荐日收盘':round(bclose,2) if bclose is not None else ''}
                cumul_last=np.nan; cumul_vals=[]
                for n,day in enumerate(selected_dates,1):
                    col=day[5:]
                    match=d[d['trade_date']==day] if not d.empty else pd.DataFrame()
                    if bclose is not None and not match.empty:
                        rr=match.iloc[-1]
                        close=float(rr['close']) if pd.notna(rr['close']) else np.nan
                        pct=float(rr['pct_chg']) if pd.notna(rr['pct_chg']) else np.nan
                        cu=(close/bclose-1)*100 if bclose and pd.notna(close) else np.nan
                        row[col]=round(pct,2) if pd.notna(pct) else ''
                        cumul_vals.append(cu)
                        detail.append({'推荐排名':rank_map.get(code,''),'推荐度':score_map.get(code,''),'代码':code,'名称':name_map.get(code,''),'推荐日':rec_date,'交易日':day,'T+N':f'T+{n}','收盘':round(close,2) if pd.notna(close) else '','当日涨跌%':round(pct,2) if pd.notna(pct) else '','累计涨幅%':round(cu,2) if pd.notna(cu) else '','数据状态':'正常'})
                    else:
                        row[col]=''
                        detail.append({'推荐排名':rank_map.get(code,''),'推荐度':score_map.get(code,''),'代码':code,'名称':name_map.get(code,''),'推荐日':rec_date,'交易日':day,'T+N':f'T+{n}','收盘':'','当日涨跌%':'','累计涨幅%':'','数据状态':'该交易日无本股数据' if bclose is not None else '缺少推荐日收盘，无法计算'})
                if cumul_vals: cumul_last=cumul_vals[-1]
                if bclose is None:
                    status='缺少推荐日收盘'; high_cu=np.nan; low_cu=np.nan
                elif d.empty:
                    status='推荐日后暂无数据'; high_cu=np.nan; low_cu=np.nan
                else:
                    closes=pd.to_numeric(d['close'],errors='coerce').dropna()
                    all_cu=(closes/bclose-1)*100 if bclose else pd.Series(dtype=float)
                    high_cu=float(all_cu.max()) if not all_cu.empty else np.nan
                    low_cu=float(all_cu.min()) if not all_cu.empty else np.nan
                    status=f'已跟踪{len(d)}个交易日'
                row['动态累计涨幅%']='' if pd.isna(cumul_last) else round(float(cumul_last),2)
                row['期间最高%']='' if pd.isna(high_cu) else round(high_cu,2)
                row['期间最低%']='' if pd.isna(low_cu) else round(low_cu,2)
                row['状态']=status
                summary.append(row)

            date_cols=[d[5:] for d in selected_dates]
            summary_cols=['代码','名称','推荐度','推荐排名','推荐日','推荐日收盘']+date_cols+['动态累计涨幅%','期间最高%','期间最低%','状态']
            summary_df=pd.DataFrame(summary)
            for c in summary_cols:
                if c not in summary_df.columns: summary_df[c]=''
            summary_df=summary_df[summary_cols]
            if not summary_df.empty:
                summary_df['_score_sort']=pd.to_numeric(summary_df['推荐度'],errors='coerce')
                summary_df['_rank_sort']=pd.to_numeric(summary_df['推荐排名'],errors='coerce')
                summary_df=summary_df.sort_values(['_score_sort','_rank_sort'],ascending=[False,True],na_position='last').drop(columns=['_score_sort','_rank_sort'])
            self.tracking_result_df=pd.DataFrame(detail)
            self.tracking_summary_df=summary_df
            self._fill_tracking_trees()
            latest_date=selected_dates[-1] if selected_dates else ''
            self.track_summary_var.set(f'推荐日期：{rec_date}｜股票：{len(codes)}只｜横向日期：{", ".join(selected_dates) if selected_dates else "无后续日线"}｜最新：{latest_date or "无"}')
            self.set_status(f'推荐股整理完成：{len(codes)}只；横向统计 {len(selected_dates)} 个交易日；原始明细 {len(self.tracking_result_df)} 行。')
        except Exception as exc:
            messagebox.showerror('推荐股跟踪失败', str(exc))

    def _fill_tracking_trees(self):
        for i in self.track_summary_tree.get_children(): self.track_summary_tree.delete(i)
        for i in self.track_detail_tree.get_children(): self.track_detail_tree.delete(i)
        if not self.tracking_summary_df.empty:
            cols=tuple(self.tracking_summary_df.columns)
            self.track_summary_tree['columns']=cols
            for c in cols:
                self.track_summary_tree.heading(c,text=c)
                self.track_summary_tree.column(c,width=78 if c in ('代码','推荐度','推荐排名') else 95,anchor=tk.CENTER)
            self.track_summary_tree.column('名称',width=105,anchor=tk.CENTER)
            self.track_summary_tree.column('状态',width=140,anchor=tk.CENTER)
            for _,r in self.tracking_summary_df.iterrows():
                self.track_summary_tree.insert('',tk.END,values=[r.get(c,'') for c in cols])
        if not self.tracking_result_df.empty:
            for _,r in self.tracking_result_df.iterrows():
                self.track_detail_tree.insert('',tk.END,values=[r.get(c,'') for c in self.track_detail_tree['columns']])

    def export_tracking_summary_csv(self):
        if self.tracking_summary_df is None or self.tracking_summary_df.empty:
            messagebox.showwarning('提示','当前没有汇总跟踪结果。'); return
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='tracking_summary.csv',filetypes=[('CSV文件','*.csv')])
        if not path:return
        self.tracking_summary_df.to_csv(path,index=False,encoding='utf-8-sig')
        messagebox.showinfo('成功',f'已导出横向汇总：\n{path}')

    def export_tracking_detail_csv(self):
        if self.tracking_result_df is None or self.tracking_result_df.empty:
            messagebox.showwarning('提示','当前没有原始明细结果。'); return
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='tracking_detail.csv',filetypes=[('CSV文件','*.csv')])
        if not path:return
        self.tracking_result_df.to_csv(path,index=False,encoding='utf-8-sig')
        messagebox.showinfo('成功',f'已导出原始明细：\n{path}')

    def export_tracking_csv(self):
        self.export_tracking_summary_csv()

    def export_csv(self):
        if self.result_df is None or self.result_df.empty:
            messagebox.showwarning('提示','当前没有筛选结果。'); return
        path = filedialog.asksaveasfilename(
            defaultextension='.csv', initialfile=f'screen_{self.current_date}.csv',
            filetypes=[('CSV文件','*.csv')]
        )
        if path:
            self.result_df.to_csv(path, index=False, encoding='utf-8-sig')
            messagebox.showinfo('成功', f'已导出：\n{path}')

    def load_on_start(self):
        try:
            with sqlite3.connect(self.dm.db_path) as conn:
                d = conn.execute('SELECT COUNT(*) FROM daily_data').fetchone()[0]
                i = conn.execute('SELECT COUNT(*) FROM intraday_snapshots').fetchone()[0]
            latest = latest_completed_local_date(self.dm, before_today=False)
            latest_text = latest or '无'
            self.daily_summary_var.set(f'日线状态：本地最新日线 {latest_text}｜共{d}条；盘中采样 {i}条')
            self.set_status(f'数据库就绪：日线{d}条，盘中采样{i}条。')
        except Exception as exc:
            self.set_status(f'数据库检查失败：{exc}')


if __name__ == '__main__':
    root = tk.Tk()
    app = StockResearchGUI(root)
    root.mainloop()
