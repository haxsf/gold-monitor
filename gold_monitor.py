#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金价监测脚本（每5分钟，触发才提醒，其余静默）
- 数据源: 新浪财经 上海黄金交易所 Au(T+D)（元/克）
- 日间(5:00~22:00): 金价 > 770 或 < 760 → 推微信(Server酱)
- 晚间(22:00~次日5:00): 金价 > 770 或 < 740 → 推微信(Server酱)
- 未触发/获取失败: 静默退出, 不发任何消息
用法: python3 gold_monitor.py [--test]   # --test 强制发送一条测试推送验证通道
"""
import sys, time, datetime, traceback
import requests

# ===== 配置 =====
SENDKEY = "SCT482805TemOddyb9es5f4OOyqrXF3q5u"
HIGH_LIMIT = 770.0
LOW_DAY = 750.0    # 日间下限
LOW_NIGHT = 740.0  # 晚间下限
DAY_START, DAY_END = 5, 22  # 日间: [5:00, 22:00)
LOG_FILE = "/workspace/gold_monitor.log"
STATE_FILE = "/workspace/.gold_state"
COOLDOWN_MIN = 0  # 0 = 取消冷却, 每次越限都推送
TIMEOUT = 15
CA = "/etc/ssl/certs/ca-certificates.crt"  # 沙箱环境需指定系统CA

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def get_gold_price():
    """获取金价(元/克)。主源: 上金所 Au(T+D); 备源: 伦敦金XAU折算。"""
    headers = {"Referer": "https://finance.sina.com.cn"}
    # 主源: Au(T+D), 字段[0]为最新价
    try:
        r = requests.get("https://hq.sinajs.cn/list=gds_AUTD", headers=headers, timeout=TIMEOUT, verify=CA)
        r.encoding = "gbk"
        parts = r.text.split('"')[1].split(",")
        price = float(parts[0])
        if price > 0:
            return price, "Au(T+D)"
    except Exception:
        pass
    # 备源: 伦敦金(美元/盎司) + 汇率 折算为 元/克
    try:
        r1 = requests.get("https://hq.sinajs.cn/list=hf_XAU", headers=headers, timeout=TIMEOUT, verify=CA)
        r1.encoding = "gbk"
        xau = float(r1.text.split('"')[1].split(",")[0])  # USD/oz
        r2 = requests.get("https://hq.sinajs.cn/list=fx_susdcny", headers=headers, timeout=TIMEOUT, verify=CA)
        r2.encoding = "gbk"
        usdcny = float(r2.text.split('"')[1].split(",")[9])
        price = xau * usdcny / 31.1035
        if price > 0:
            return round(price, 2), "伦敦金折算"
    except Exception:
        pass
    return None, None

def push_wechat(title, desp):
    r = requests.post(
        f"https://sctapi.ftqq.com/{SENDKEY}.send",
        data={"title": title[:32], "desp": desp},
        timeout=TIMEOUT, verify=CA)
    return r.json().get("code") == 0

def main():
    test = "--test" in sys.argv
    now = datetime.datetime.now()
    is_day = DAY_START <= now.hour < DAY_END
    low = LOW_DAY if is_day else LOW_NIGHT
    period = "日间" if is_day else "晚间"

    if test:
        ok = push_wechat("金价监测·通道测试",
                         "✅ 脚本监测通道正常\n\n阈值规则不变：日间(5~22点) >770/<760，晚间 >770/<740。")
        log(f"TEST push {'OK' if ok else 'FAILED'}")
        print("test push:", "OK" if ok else "FAILED")
        return

    price, source = get_gold_price()
    if price is None:
        log("SILENT (price unavailable)")
        return

    if price > HIGH_LIMIT:
        trig = f"已超过上限 {HIGH_LIMIT:g} 元/克"
    elif price < low:
        trig = f"已跌破{period}下限 {low:g} 元/克"
    else:
        log(f"SILENT ({period}) price={price} source={source}")
        return

    t = now.strftime("%H:%M")

    # 冷却: 持续越限期间, 同方向30分钟内不重复推送
    import os, json
    last = {}
    try:
        with open(STATE_FILE) as f:
            last = json.load(f)
    except Exception:
        pass
    last_ts = last.get("last_push_ts", 0)
    last_dir = last.get("last_dir", "")
    direction = "HIGH" if price > HIGH_LIMIT else "LOW"
    if COOLDOWN_MIN > 0 and last_dir == direction and now.timestamp() - last_ts < COOLDOWN_MIN * 60:
        log(f"SUPPRESS ({period}) price={price} trig={trig} (冷却中)")
        return
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_push_ts": now.timestamp(), "last_dir": direction}, f)
    except Exception:
        pass

    title = f"金价提醒:{price:g}元/克 {trig}"
    desp = f"**当前金价:** {price:g} 元/克（{source}）\n\n**时间:** {t}\n\n**触发:** {trig}"
    ok = push_wechat(title, desp)
    log(f"ALERT ({period}) price={price} trig={trig} push={'OK' if ok else 'FAILED'}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR " + traceback.format_exc().replace("\n", " | "))
