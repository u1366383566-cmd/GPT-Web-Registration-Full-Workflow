"""已安装协议后端的桌面浏览器指纹目录及基础 HTTP 画像同步。"""
import re
from typing import get_args

PROFILE_FIELDS = {'CURL_CFFI_IMPERSONATE': 'curl_cffi', 'WREQ_EMULATION': 'wreq'}


def supported_profiles(backend):
    """不暴露版本漂移别名、移动端或非浏览器协议，避免与桌面 JS 环境混用。"""
    if backend == 'curl_cffi':
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        return [name for name in get_args(BrowserTypeLiteral)
                if re.fullmatch(r'(?:chrome\d+a?|edge\d+|firefox\d+|safari\d+)', name)]
    if backend == 'wreq':
        try:
            from wreq import Profile
        except ImportError:
            return []
        return [name.lower() for name in dir(Profile)
                if re.fullmatch(r'(?:Chrome\d+|Edge\d+|Firefox\d+|Safari[\d_]+)', name)]
    raise ValueError(f'未知协议 HTTP 客户端: {backend}')


def profile_choices(key):
    return [{'value': name, 'label': name} for name in supported_profiles(PROFILE_FIELDS[key])]


def validate_protocol_profile(backend, name):
    if name not in supported_profiles(backend):
        raise ValueError(f'{backend} 未安装或不支持桌面浏览器协议 {name}，请安装依赖并在 Web 配置中重新选择')
    return name


def align_browser_profile(profile, name):
    """同步 UA、浏览器系列和基本 Client Hints；不是完整浏览器运行时模拟。"""
    profile = dict(profile)
    family, version = re.fullmatch(r'(chrome|edge|firefox|safari)([\d_a]+)', name).groups()
    major = re.match(r'\d+', version).group()
    if family == 'safari':
        # curl_cffi 用 safari184 / safari2601；wreq 用 safari18_3 / safari26_1。
        version = version.replace('_', '.') if '_' in version else (version[:2] + '.' + '.'.join(version[2:]) if len(version) > 2 else version + '.0')
    else:
        version = f'{major}.0.0.0'
    profile.update(browser_family=family, browser_os='macOS', navigator_platform='MacIntel',
                   user_agent_data_platform='macOS', sec_ch_ua_platform='"macOS"',
                   send_client_hints=family in ('chrome', 'edge'),
                   navigator_vendor='Apple Computer, Inc.' if family == 'safari' else '' if family == 'firefox' else 'Google Inc.')
    if family in ('chrome', 'edge'):
        # 默认 Chrome146 保持工程现有品牌顺序，其他版本仅同步基本版本信息。
        profile.update(chrome_major=major, chrome_full_version=version,
                       safari_version='', safari_webkit_version='537.36')
        brand = 'Microsoft Edge' if family == 'edge' else 'Google Chrome'
        profile['sec_ch_ua'] = f'"Chromium";v="{major}", "Not-A.Brand";v="24", "{brand}";v="{major}"'
        profile['sec_ch_ua_full_version_list'] = f'"Chromium";v="{version}", "Not-A.Brand";v="24.0.0.0", "{brand}";v="{version}"'
        profile['user_agent'] = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                 f'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36'
                                 + (f' Edg/{version}' if family == 'edge' else ''))
    else:
        profile.update(sec_ch_ua='', sec_ch_ua_full_version_list='', sec_ch_ua_mobile='?0',
                       chrome_major='', chrome_full_version='')
        if family == 'safari':
            profile.update(safari_version=version, safari_webkit_version='605.1.15')
            profile['user_agent'] = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                    f'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version} Safari/605.1.15')
        else:
            profile.update(safari_version='', safari_webkit_version='')
            profile['user_agent'] = (f'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{major}.0) '
                                    f'Gecko/20100101 Firefox/{major}.0')
    return profile
