"""Write the repair-intake test set: 30 scripted enquiries. Each has an opening message,
the answer the customer gives when asked about a field (absent = the customer goes
quiet), and what a careful person would have written down. All invented.
Week of Monday 16 November 2026; times are Taipei time.
"""
import json
from pathlib import Path

E = []


def add(at, opening, expect="ready", answers=None, **truth):
    E.append(dict(id=len(E) + 1, at=f"2026-11-{at}:00+08:00", user=f"U{len(E) + 1:032x}", opening=opening,
                  answers=answers or {}, expect=expect, truth=truth))


add("16T09:10", "你好，我家冷氣不冷，在台中市西屯區，週三下午可以來嗎", area="台中市西屯區", equipment="冷氣", problem_has="不冷", slot_date="2026-11-18", slot_part="下午")
add("16T20:40", "冰箱不冷了", answers=dict(area="高雄市鳳山區", slot_text="明天上午"), area="高雄市鳳山區", equipment="冰箱", problem_has="不冷", slot_date="2026-11-17", slot_part="上午")
add("16T22:15", "請問洗衣機維修", answers=dict(area="新北市板橋區", problem="脫水的時候很吵，而且會漏水", slot_text="11/19 下午"), area="新北市板橋區", equipment="洗衣機", problem_has="漏水", slot_date="2026-11-19", slot_part="下午")
add("17T07:50", "熱水器點不著，台南市東區，今天下午或明天上午都可以", area="台南市東區", equipment="熱水器", problem_has="點不著", slot_date=None, slot_part=None)
add("17T10:05", "家裡有瓦斯味，熱水器好像漏氣", expect="emergency")
add("17T11:30", "冷氣漏水，多少錢？", answers=dict(area="桃園市中壢區", slot_text="週五上午"), area="桃園市中壢區", equipment="冷氣", problem_has="漏水", slot_date="2026-11-20", slot_part="上午", asks_price=True)
add("17T13:00", "抽油煙機不轉了", answers=dict(area="彰化縣員林市", slot_text="下週一下午"), area="彰化縣員林市", equipment="抽油煙機", problem_has="不轉", slot_date="2026-11-23", slot_part="下午")
add("17T19:30", "飲水機不出熱水 新竹市東區 平日晚上", area="新竹市東區", equipment="飲水機", problem_has="不出熱水", slot_date=None, slot_part="晚上")
add("18T09:00", "你好", answers=dict(area="台北市大安區", equipment="除濕機", problem="開機沒多久就自動關掉", slot_text="後天下午"), area="台北市大安區", equipment="除濕機", problem_has="自動關掉", slot_date="2026-11-20", slot_part="下午")
add("18T12:20", "插座冒煙有焦味！冷氣一開就這樣", expect="emergency")
add("18T14:00", "我要修電視", answers=dict(area=["在逢甲附近", "台中"], problem="畫面有線條", slot_text="都可以"), area="台中", equipment="電視", problem_has="線條", slot_date=None, slot_part=None)
add("18T21:05", "洗碗機顯示E24錯誤，新北市新店區，週六上午", area="新北市新店區", equipment="洗碗機", problem_has="E24", slot_date="2026-11-21", slot_part="上午")
add("19T08:00", "乾衣機不熱 台中市北屯區", answers=dict(slot_text="11/21 下午2點"), area="台中市北屯區", equipment="烘衣機", problem_has="不熱", slot_date="2026-11-21", slot_part="下午")
add("19T10:10", "冰箱結霜很嚴重", answers=dict(area="宜蘭縣羅東鎮", slot_text="下週三"), area="宜蘭縣羅東鎮", equipment="冰箱", problem_has="結霜", slot_date="2026-11-25", slot_part=None)
add("19T15:45", "請問你們有修製冰機嗎", answers=dict(area="高雄市左營區", equipment="製冰機", problem="不會製冰", slot_text="明天下午"), area="高雄市左營區", equipment="製冰機", problem_has="不會製冰", slot_date="2026-11-20", slot_part="下午")
add("19T23:50", "冷氣遙控器沒反應，機器也不動。花蓮縣吉安鄉。", expect="abandoned")
add("20T09:30", "洗衣機壞了", expect="incomplete", answers=dict(area=["不方便說", "見面再說"]))
add("20T11:00", "熱水器忽冷忽熱，是不是主機板壞了？台中市南屯區，下午都在", area="台中市南屯區", equipment="熱水器", problem_has="忽冷忽熱", slot_date=None, slot_part="下午", asks_price=True)
add("20T17:59", "電風扇不轉 屏東縣屏東市 明天早上", area="屏東縣屏東市", equipment="電風扇", problem_has="不轉", slot_date="2026-11-21", slot_part="上午")
add("20T18:30", "冷氣有異音，台北市內湖區，下週二晚上7點以後", area="台北市內湖區", equipment="冷氣", problem_has="異音", slot_date="2026-11-24", slot_part="晚上")
add("21T10:00", "微波爐會跳電", answers=dict(area="基隆市仁愛區", slot_text="今天下午"), area="基隆市仁愛區", equipment="微波爐", problem_has="跳電", slot_date="2026-11-21", slot_part="下午")
add("21T14:20", "冰箱門關不緊，東西都退冰了，苗栗縣頭份市，週一到週五白天", answers=dict(problem="門關不緊，冷藏的東西都退冰了"), area="苗栗縣頭份市", equipment="冰箱", problem_has="關不緊", slot_date=None, slot_part="白天")
add("21T19:00", "暖氣機有燒焦味", expect="emergency")
add("22T10:00", "洗衣機不排水，南投縣草屯鎮", answers=dict(slot_text="週二上午"), area="南投縣草屯鎮", equipment="洗衣機", problem_has="不排水", slot_date="2026-11-24", slot_part="上午")
add("22T15:30", "分離式冷氣室外機很吵，雲林縣斗六市，11/25 上午", area="雲林縣斗六市", equipment="冷氣", problem_has="很吵", slot_date="2026-11-25", slot_part="上午")
add("16T16:00", "想問一下冷氣保養怎麼收費", expect="abandoned", answers=dict(area=["只是問價錢"]), asks_price=True)
add("17T16:30", "瓦斯爐點不著火", answers=dict(area="台中市大里區", slot_text="週四下午"), area="台中市大里區", equipment="瓦斯爐", problem_has="點不著", slot_date="2026-11-19", slot_part="下午")
add("18T10:40", "臺北市信義區，烘衣機轉一下就停，這週六下午", answers=dict(problem="烘衣機轉一下就停了"), area="台北市信義區", equipment="烘衣機", problem_has="就停", slot_date="2026-11-21", slot_part="下午")
add("19T12:00", "冷氣不冷 高雄市三民區 12/3 上午", area="高雄市三民區", equipment="冷氣", problem_has="不冷", slot_date="2026-12-03", slot_part="上午")
add("20T20:10", "熱水器漏水 嘉義市西區", answers=dict(slot_text="隨時"), area="嘉義市西區", equipment="熱水器", problem_has="漏水", slot_date=None, slot_part=None)

out = Path(__file__).parent.parent / "fixtures" / "repairs"
out.mkdir(parents=True, exist_ok=True)
(out / "enquiries.json").write_text(json.dumps(dict(
    note="Invented. expect: ready = all four fields collected; emergency = must go to a person at once; "
         "incomplete = the customer would not say; abandoned = the customer stopped answering.",
    enquiries=E), ensure_ascii=False, indent=1))
print(len(E), "enquiries;", {k: sum(e["expect"] == k for e in E) for k in ("ready", "emergency", "incomplete", "abandoned")})
