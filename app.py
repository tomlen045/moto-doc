#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大排诊脉 MotoDoc · 大排量摩托故障问诊引擎
输入：症状描述（自由文本）+ 车型 + 里程 → 输出：概率排序病因 + DIY可行性 + 配件价区间 + AI 问诊清单
病因规则来自摩托车机械通用知识 + 车型社区高频问题（如450SR链条松弛为海外车主实测反馈），禁 Mock 编造。
"""
import json
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
OUT_DIR = os.path.join(BASE, "output")
STATIC = os.path.join(BASE, "static")
for d in (DATA_DIR, OUT_DIR, STATIC):
    os.makedirs(d, exist_ok=True)

DIY_LABEL = {0: "🟢 自己动手", 1: "🟡 要工具和经验", 2: "🔴 别自己拆 · 找授权师傅"}

BIKES = {
    "cf450sr": dict(name="春风450SR", mileage_ref=8000,
                    note="450SR 链条松弛是社区高频反馈（海外车主实测约每月需重新张紧），每次保养先查链条张度"),
    "wl525r": dict(name="无极525R", mileage_ref=10000,
                   note="525R 双缸高转特性，气门间隙检查周期别超手册上限"),
    "qj600": dict(name="钱江赛600", mileage_ref=8000,
                  note="四缸车怠速不稳/多缸同步问题优先排查，节气门积碳概率高于双缸车"),
    "cf800mt": dict(name="春风800MT", mileage_ref=15000,
                    note="800MT 车重大，轮胎偏磨与链条磨损速度快于同级，胎压务必每次骑行前查"),
    "nk800": dict(name="春风800NK", mileage_ref=12000,
                  note="街车骑姿前轮负载大，前减震油封漏油在坑洼路骑行后高发"),
}


def C(name, w, diy, parts, note):
    return dict(name=name, w=w, diy=diy, parts=parts, note=note)


def R(kw, causes, advice):
    return dict(kw=kw, causes=causes, advice=advice)


RULES = [
    R(["链条", "异响"], [
        C("链条过松/缺油磨损", 5, 0,
          [("链条油", "30-80"), ("链条套装（链盘三件）", "300-900")],
          "先用链条尺量松弛度（一般10-25mm），顺手补油试骑5公里复听"),
    ], "大排链条每500km一查一油，松了先张紧，磨出尖齿直接换套装"),
    R(["咔哒", "起步", "加速"], [
        C("链轮牙盘磨损（猴爬杆）", 4, 1,
          [("前小链轮", "50-150"), ("后牙盘", "150-400")],
          "后牙盘齿尖磨成钩状=必须三件套一起换，只换单项加速磨损"),
    ], "起步咔哒+加速发咬，链传动系统磨损的典型声纹"),
    R(["嗒嗒", "气门", "怠速", "异响"], [
        C("气门间隙偏大", 4, 2,
          [("气门室垫片", "80-300"), ("工时费", "300-800")],
          "热车后缸头嗒嗒声随转速变化，需塞尺逐缸测量调整，动手难度高"),
        C("张紧器失效", 2, 1,
          [("自动张紧器总成", "120-400")],
          "冷启动前几秒金属摩擦嘶嘶声+嗒嗒，张紧器顶不住正时链"),
    ], "缸头异响别拖，气门间隙超标会烧气门座，修起来翻十倍"),
    R(["抖", "高速", "龙头", "晃"], [
        C("轮胎动平衡失准/胎压异常", 4, 0,
          [("动平衡（路边店）", "20-50"), ("胎压监测补气", "0-20")],
          "先查胎压（冷胎按贴纸值），补铅块动平衡10分钟解决"),
        C("前轮胎变形/鼓包", 3, 2,
          [("前轮胎", "400-1200")],
          "轮胎侧壁鼓包=爆胎前兆，立刻停骑换胎"),
        C("前轴承旷量", 2, 2,
          [("前轮轴承一对", "60-200"), ("工时费", "100-300")],
          "支起前轮抓轮缘前后晃，有旷量就是轴承，别凑合"),
    ], "高速抖动先排除最便宜的胎压/动平衡，再往轮胎、轴承查"),
    R(["怠速", "不稳", "游车"], [
        C("节气门/怠速阀积碳", 4, 1,
          [("节气门清洗剂", "30-60"), ("工时费（店洗）", "100-300")],
          "电喷车怠速游车多因积碳，拆进气管清洗+怠速学习复位"),
        C("火花塞老化", 3, 0,
          [("铱金火花塞", "40-120/只")],
          "按手册周期更换，多缸车建议整套换并量点火线圈阻值"),
    ], "怠速不稳先洗节气门再换火花塞，都是小钱，别先怀疑电喷主板"),
    R(["热车", "熄火"], [
        C("汽油泵热衰减", 3, 2,
          [("燃油泵总成", "300-900")],
          "凉车正常热车熄火、凉半小时又能着=油泵典型热衰减，别在路上赌"),
        C("点火线圈热衰减", 3, 1,
          [("点火线圈", "80-300/只")],
          "熄火前先有顿挫再灭=点火系统，逐缸换线圈试"),
    ], "热衰减类故障有规律（温度相关），记录熄火时的水温/气温供师傅判断"),
    R(["雨天", "进水", "洗车", "打不着"], [
        C("点火系统/插头进水", 4, 0,
          [("电子清洁剂", "20-50")],
          "断电后拔高压帽和线束插头吹干喷清洁剂，晒半天基本复活"),
        C("空滤进水", 2, 1,
          [("空滤", "50-200")],
          "涉水后怠速发闷+加速无力=查空滤是否吸水，湿了必须换"),
    ], "雨天/洗车后趴窝，九成是电，别急着叫拖车，先断电晾干"),
    R(["冷启动", "难启动", "冬天"], [
        C("电瓶亏电/老化", 5, 0,
          [("锂电池/AGM电瓶", "200-900")],
          "测静息电压：<12.4V先充电，充不进或一年以上直接换"),
        C("启动电机碳刷", 2, 2,
          [("启动电机总成", "300-800")],
          "电压正常但启动'咔'一声没反应=启动电机或继电器"),
    ], "难启动先电瓶后油路，电瓶寿命普遍2-3年，冬天是照妖镜"),
    R(["无力", "高转", "跑不快"], [
        C("空滤堵塞", 4, 0,
          [("空滤", "50-200")],
          "高转无力先查空滤，风沙/柳絮季要提前换，几十块钱的事"),
        C("汽油滤/油路脏", 3, 1,
          [("汽油滤", "30-120")],
          "加过小站油的优先排查，滤芯脏会连带伤油泵"),
    ], "动力衰减三件套：空滤→汽油滤→火花塞，从便宜到贵按顺序来"),
    R(["顿挫", "收油", "低转"], [
        C("点火正时/火花塞性能衰减", 3, 0,
          [("铱金火花塞", "40-120/只")],
          "低转顿挫高转顺=点火弱，整套换火花塞立竿见影"),
        C("节气门积碳", 3, 1,
          [("节气门清洗剂", "30-60")],
          "与怠速游车同源，洗完做电喷复位"),
    ], "低转顿挫别急着改排气刷ECU，先恢复原厂状态再谈调校"),
    R(["蓝烟", "烧机油", "冒烟"], [
        C("活塞环/气门油封磨损", 4, 2,
          [("气门油封套装", "100-300"), ("大修工时", "1500-4000")],
          "收油门瞬间蓝烟=气门油封；持续蓝烟=活塞环，都得开缸找师傅"),
    ], "烧机油唯一正确姿势是尽早开缸，拖着烧会积碳拉缸，费用翻倍"),
    R(["漏油", "前减", "减震"], [
        C("前减震油封漏油", 5, 2,
          [("油封一对", "40-150"), ("减震油+工时", "200-500")],
          "减震筒挂油珠=油封，继续骑会磨坏内筒（内筒比油封贵十倍）"),
    ], "前减漏油别拖，油封便宜内筒贵，刹车盘被油沾到还会失制动力"),
    R(["水温", "开锅", "风扇"], [
        C("冷却液不足/变质", 4, 0,
          [("冷却液", "40-100")],
          "冷车查副水箱液位，缺了先补同型号，两天就缺=找漏点"),
        C("节温器/水泵故障", 3, 2,
          [("节温器", "80-250"), ("水泵", "200-600")],
          "上水管不热=节温器没开；水温飙+水音异响=水泵叶轮腐蚀"),
    ], "高温行驶会拉缸，水温报警立即停车熄火，别赌下一个路口"),
    R(["ABS", "抱死"], [
        C("轮速传感器脏污/间隙异常", 4, 0,
          [("电子清洁剂", "20-50")],
          "ABS灯常亮但刹车正常，八成是传感器头吸了铁屑，拆下清洁复位"),
        C("传感器/泵故障", 2, 2,
          [("轮速传感器", "150-500"), ("ABS泵", "2000-6000")],
          "清洁无效再读故障码定位，ABS泵贵，先便宜后贵"),
    ], "ABS灯亮=系统已退出保护，刹车逻辑变差，尽快修别拆ABS保险硬骑"),
    R(["亏电", "没电", "充电"], [
        C("整流器故障（不充电）", 4, 1,
          [("整流稳压器", "100-400")],
          "行驶中电压<13V=整流器罢工，换新前量充电电压14-14.5V为正常"),
        C("电瓶老化", 3, 0,
          [("电瓶", "200-900")],
          "两年以上电瓶夜间刹车灯一亮就启动无力，直接换"),
    ], "频繁亏电别光换电瓶，先量充电电压，整流器坏了换几个电瓶都白搭"),
    R(["轮胎", "锯齿", "偏磨"], [
        C("胎压长期异常+骑行习惯", 5, 0,
          [("胎压表", "20-60")],
          "前轮中央锯齿=高速重刹习惯；单侧磨损=胎压不对或骑姿偏，养成每周量胎压"),
    ], "轮胎是唯一接触地面的部件，锯齿胎雨天抓地力骤降，磨到磨标就换"),
]

GENERIC_AI_Q = [
    "我的{bike}，行驶{km}公里，症状：{sym}。请按概率从高到低列出可能原因、判断依据、DIY可行性分级，并给出从便宜到贵的排查顺序，标注哪一步绝对不能自己动手。",
    "我要带车去修理店处理这个问题：请给我一张检查清单（应备工具/备件/参考工时费），以及师傅报价时的三个反问，帮我识别过度维修。",
    "如果最终确诊为「{top}」，请给出原厂件与副厂件的价格对比、修复vs更换的决策建议，以及修复后验收标准。",
]


def analyze(symptoms, bike_key, mileage):
    b = BIKES.get(bike_key)
    if not b or not symptoms.strip():
        return None
    text = symptoms
    scored = {}
    matched_groups = []
    for rule in RULES:
        hits = [k for k in rule["kw"] if k in text]
        if not hits:
            continue
        matched_groups.append({"keywords": hits, "advice": rule["advice"]})
        for c in rule["causes"]:
            if c["name"] not in scored:
                scored[c["name"]] = {"w": 0, "c": c}
            scored[c["name"]]["w"] += c["w"] * len(hits)
    if not scored:
        return {"bike": b["name"], "no_match": True, "note": b["note"],
                "groups": [], "causes": [], "mileage": mileage, "symptoms": symptoms,
                "ai_questions": ["我的%s，行驶%s公里，症状：%s。请按概率列出可能原因与排查顺序。"
                                 % (b["name"], mileage, symptoms)]}
    total = sum(v["w"] for v in scored.values())
    causes = []
    for v in sorted(scored.values(), key=lambda x: -x["w"]):
        c = v["c"]
        causes.append({
            "name": c["name"], "prob": round(v["w"] / total * 100, 1),
            "diy": c["diy"], "diy_label": DIY_LABEL[c["diy"]],
            "parts": [list(p) for p in c["parts"]], "note": c["note"],
        })
    top = causes[0]["name"]
    qs = [t.replace("{bike}", b["name"]).replace("{km}", str(mileage))
          .replace("{sym}", symptoms).replace("{top}", top) for t in GENERIC_AI_Q]
    return {"bike": b["name"], "no_match": False, "causes": causes,
            "groups": matched_groups, "note": b["note"], "ai_questions": qs,
            "mileage": mileage, "symptoms": symptoms}


def render_doc(res):
    L = []
    L.append("# %s · 故障问诊单" % res["bike"])
    L.append("")
    L.append("- **症状**：%s" % res["symptoms"])
    L.append("- **里程**：%s km　**生成日期**：%s" % (res["mileage"], time.strftime("%Y-%m-%d")))
    L.append("- **车型提示**：%s" % res["note"])
    L.append("")
    L.append("## 病因概率排序")
    for i, c in enumerate(res["causes"], 1):
        L.append("%d. **%s**（相对概率 %s%%）　%s" % (i, c["name"], c["prob"], c["diy_label"]))
        L.append("   - 处置：%s" % c["note"])
        for pn, pr in c["parts"]:
            L.append("   - %s：¥%s" % (pn, pr))
    L.append("")
    L.append("## 问 AI 的深度排查清单")
    for i, q in enumerate(res["ai_questions"], 1):
        L.append("%d. %s" % (i, q))
    L.append("")
    L.append("---")
    L.append("*大排诊脉 MotoDoc · 病因为概率判断非确诊，安全件（刹车/轮胎/减震/ABS）一律以实车检修为准*")
    return "\n".join(L)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
                return self._send(200, f.read())
        if path == "/api/meta":
            return self._send(200, json.dumps({"bikes": {k: {"name": v["name"], "note": v["note"],
                                                             "mileage_ref": v["mileage_ref"]}
                                                         for k, v in BIKES.items()}},
                                              ensure_ascii=False), "application/json; charset=utf-8")
        m = re.match(r"^/download/([0-9a-f-]{36})\.md$", path)
        if m:
            fp = os.path.join(OUT_DIR, m.group(1) + ".md")
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/markdown; charset=utf-8",
                                      {"Content-Disposition": "attachment; filename=doc.md"})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/diagnose":
            return self._send(404, "not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            bk = data.get("bike", "")
            if bk not in BIKES:
                return self._send(400, json.dumps({"ok": False, "error": "bike 无效"}),
                                  "application/json; charset=utf-8")
            symptoms = str(data.get("symptoms", ""))[:500]
            mileage = int(data.get("mileage") or 0)
            res = analyze(symptoms, bk, mileage)
            if not res:
                return self._send(400, json.dumps({"ok": False, "error": "症状描述为空"}),
                                  "application/json; charset=utf-8")
            pid = str(uuid.uuid4())
            doc = render_doc(res)
            with open(os.path.join(OUT_DIR, pid + ".md"), "w", encoding="utf-8") as f:
                f.write(doc)
            with open(os.path.join(DATA_DIR, pid + ".json"), "w", encoding="utf-8") as f:
                json.dump({"id": pid, "created": time.strftime("%Y-%m-%d"),
                           "input": data, "result": res}, f, ensure_ascii=False, indent=1)
            return self._send(200, json.dumps({"ok": True, "id": pid, "result": res,
                                               "doc": doc, "download": "/download/%s.md" % pid},
                                              ensure_ascii=False),
                              "application/json; charset=utf-8")
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": str(e)}),
                              "application/json; charset=utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "8691"))
    print("MotoDoc running at http://127.0.0.1:%d" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
