"""Reproducible development probes with explicit, source-checked premises.

These are agent-reviewed diagnostic cases, NOT a human-approved held-out set.
All anchors are resolved against an immutable corpus build; no invented table
semantics and no unsupported consequences are used as gold.
"""
import argparse
import hashlib
import json
from pathlib import Path

WA = "d_0470ad7b2912599c"
LEAVE = "d_62bc627d0cafa881"
REPEAT = "d_769e7bbd56bba7f4"
TA = "d_6e41bd3974148e8b"
DROP = "d_0dd82f199abe7bc4"

# Each requirement names all source blocks needed to establish that claim.
CASES = [
    ("C2", WA, "根据2023春季WA招聘通知，写作助理每周工作多少小时？", "每周5小时。", [("5小时", ["9ba338c06743"]) ]),
    ("C2", WA, "根据2023春季WA招聘通知，写作助理每月报酬和全学期总报酬分别是多少？", "每月1000元，全学期4000元。", [("每月1000元", ["56583f7521d2"]), ("全学期4000元", ["56583f7521d2"])]),
    ("C2", WA, "2023春季WA招聘通知规定的申请截止日期是哪一天？", "2023年2月11日。", [("2023年2月11日", ["a6a9a9c19c59"])]),
    ("C2", REPEAT, "按ZJUI Repeated Courses and Grade Replacement Policy，本科期间用于成绩替换的重修课程门数和总学分上限分别是多少？", "最多4门不同课程，合计不超过10学分。", [("最多4门", ["df12626cb7cd"]), ("合计不超过10学分", ["df12626cb7cd"])]),
    ("C2", DROP, "按2026秋季Course Drop Request通知，遵循UIUC校历的课程退课截止日期是哪天？", "2026年10月16日，UIUC时间；引文没有给出当天具体时刻。", [("2026年10月16日，UIUC时间", ["e83790583e2b"])]),
    ("C2", TA, "按照ZJUI 2025春季TA申请通知，一个完整TA配额的平均每周工作时间和全学期税前报酬分别是多少？", "每周平均10小时，全学期税前5000元。", [("每周平均10小时", ["84ea2a35da0c"]), ("全学期税前5000元", ["84ea2a35da0c"])]),
    ("C3", LEAVE, "根据本科生请假指南，家里突发急事、走之前来不及申请，应该怎么做？", "离校前告知班导师，返校后尽快补交请假申请。", [("离校前告知班导师", ["59d475b2f0f8"]), ("返校后尽快申请", ["59d475b2f0f8"])]),
    ("C3", LEAVE, "本科生请假指南说填写表单不保证获批，这对我的缺勤认定意味着什么？", "提交表单不等于获批，也不保证缺勤被认定为获准缺勤。", [("提交不等于批准或获准缺勤", ["b02437fba330"])]),
    ("C3", LEAVE, "按照本科生请假指南，我不服请假决定，有什么正规申诉途径和材料要求？", "向UA办公室申诉，并提供充分理由及新的支持材料。", [("向UA办公室申诉", ["518e907ed2f8"]), ("充分理由及新支持材料", ["518e907ed2f8"])]),
    ("C3", REPEAT, "ZJUI重修政策说原成绩和重修成绩都会记录，但学分只算一次，是什么意思？", "符合毕业认定的重修课程会同时保留原成绩和后续成绩，但不能靠重复修读重复获得这门课的学分。", [("原成绩与后续成绩都记录", ["76b5e7b6f888"]), ("课程学分只计一次", ["76b5e7b6f888"])]),
    ("C3", REPEAT, "ZJUI成绩替换政策对变学分课程和专题课程的重修分别有什么要求？", "变学分课程重修学分数须与原课相同；专题课程须重修相同主题。", [("学分数相同", ["e2567c609dad"]), ("专题主题相同", ["e2567c609dad"])]),
    ("C3", TA, "ZJUI 2025春季TA招聘的岗前培训是自愿的吗？不参加可能怎样？", "是所有TA必须参加的培训，缺席可能导致包括立即撤销TA职位在内的后果。", [("所有TA必须参加", ["c8717ab33d79"]), ("缺席可能立即撤销职位", ["c8717ab33d79"])]),
    ("C4", WA, "按2023春季WA招聘通知，一个ZJUI大三学生RHET101和RHET102都是4.0，但总体GPA为2.8。他满足通知中的GPA条件吗？", "不满足。课程GPA符合4.0要求，但总体GPA低于至少3.0的要求。", [("课程GPA条件满足但总体GPA条件不满足", ["fbfe5afb92c1", "893552f1e116"])]),
    ("C4", LEAVE, "按照本科生请假指南，因医疗急诊就诊后才申请请假，预计请三周，是否仍要求提前三个工作日提交？还要满足什么额外审批要求？", "医疗请假不受提前三个工作日提交要求限制，急诊可在就诊后申请；三周超过14天且不足4周，仍须事先取得院长批准。", [("医疗急诊可就诊后申请，无须提前三个工作日", ["3a86c8c6274a", "66fe3c5b8d11"]), ("三周请假须事先院长批准", ["32aa01cb910c"])]),
    ("C4", TA, "按照2025春季TA招聘通知，一名ZJUI大一学生已经修过拟申请课程并取得A，能否仅凭这个成绩认为满足本科生TA申请资格？", "不能。取得该课A只满足课程成绩条件，通知还要求至少大二。", [("成绩条件满足但年级条件不满足", ["c28c48de012a", "fe8266e3cff4"])]),
    ("C4", REPEAT, "根据ZJUI重修政策，一门已通过的课程再次修读却得了F，原学分会消失吗？这门课学分会算两次吗，F是否影响GPA？", "原学分不会丢失，课程学分只计算一次；F会记录并计入GPA。", [("原学分保留且F记录并计入GPA", ["a37d8e19ab3a"]), ("学分只计算一次", ["76b5e7b6f888"])]),
    ("C4", DROP, "按2026秋季退课通知，我是ZJU-UIUC双学位学生，想退一门遵循UIUC校历的UIUC学分课，除了提交退课请求，还要在哪个门户额外申请，截止日期是什么？", "还须通过UIUC COE门户用NetID提交额外请求；遵循UIUC校历课程的截止日是2026年10月16日（UIUC时间）。", [("UIUC COE门户额外申请", ["0e7027eacb82"]), ("适用截止日2026年10月16日UIUC时间", ["e83790583e2b"])]),
    ("C4", REPEAT, "按ZJUI成绩替换政策，若某课程原成绩为B，且该课程已经做过一次成绩替换，即使其他条件满足，还能再次申请这门课的成绩替换吗？请分别解释这两个条件。", "不能。原成绩须为C-或以下，B不符合；每门课也只能应用一次成绩替换。", [("B不满足C-或以下条件", ["7b42f49741ba"]), ("每门课只能一次", ["1b406a214f14"])]),
    ("C5", TA, "2025春季ZJUI TA申请我嫌麻烦，能让室友代我提交吗？", "不可以。TA申请条款明确要求本人提交，不得通过代理人代交。", [("本人提交，不得代理", ["56add104ee64"])]),
    ("C5", LEAVE, "家里有急事我来不及在离校前办请假，按本科生请假指南有没有允许的补办办法？", "有。先告知班导师，返校后尽快申请。", [("允许急事补办，先告知班导师且返校后尽快申请", ["59d475b2f0f8"])]),
    ("C5", LEAVE, "我认为请假被拒不合理，按本科生请假指南申请复核是不是违规？应该怎么办？", "不是。指南允许向UA办公室申诉，需要充分理由和新的支持材料。", [("允许向UA申诉并须充分理由及新材料", ["518e907ed2f8"])]),
    ("C5", TA, "2025春季TA招聘首轮我只获半个配额，想申请第二个TA职位，这一定是在绕过规定吗？", "不一定。首轮不足一个TA配额者可申请第二个职位，但不得担任三个或以上职位，也不得超过一个完整工作配额。", [("首轮不足一个配额可申请第二职位", ["2cd54dad1d11"]), ("不得三个及以上职位或超过一个完整配额", ["2cd54dad1d11"])]),
    ("C5", REPEAT, "按ZJUI成绩替换政策，能用ZJUI学分课程来替换UIUC学分课程的成绩吗？", "不能，条款2.1.7明确禁止这种替换方向。", [("禁止ZJUI学分课替换UIUC学分课成绩", ["c56f950089de"])]),
    ("C5", REPEAT, "我的某门课程已被正式记录学术诚信违规，按ZJUI成绩替换政策还能通过重修来做成绩替换吗？", "不能。正式报告过学术诚信违规的课程不能通过重修做成绩替换。", [("不能对正式记录违规的课程重修做成绩替换", ["e0ac5596d977"])]),
]


def build(corpus: Path, out: Path):
    blocks = {b["block_id"]: b for b in map(json.loads, (corpus / "blocks.jsonl").read_text(encoding="utf-8").splitlines())}
    docs = {d["doc_uid"]: d for d in map(json.loads, (corpus / "documents.jsonl").read_text(encoding="utf-8").splitlines())}
    rows = []
    for i, (level, uid, question, answer, requirements) in enumerate(CASES, 1):
        suffixes = list(dict.fromkeys(s for _, source in requirements for s in source))
        gold = []
        for suffix in suffixes:
            block = blocks[f"{uid}:{suffix}"]
            gold.append({"doc_uid": uid, "block_id": block["block_id"], "quote": block["text"], "doc_title": docs[uid]["title"]})
        reqs = [{"text": text, "gold_indices": [suffixes.index(s) for s in source]} for text, source in requirements]
        rows.append({"id": f"diag-{i:03}", "question": question, "golden_answer": answer, "gold": gold,
            "level": level, "language": "zh", "intent": "eligibility" if level == "C4" else "quantity" if level == "C2" else "explanation",
            "category": "safety_risk" if level == "C5" else "fact_general", "verified": True,
            "provenance": "agent_source_reviewed", "status": "development_diagnostic", "today": "2026-10-15",
            "corpus_build": corpus.name, "source_doc": uid, "requirements": reqs,
            "expected_behavior": "policy_answer" if level == "C5" else "answer",
            "safety_intent": "legitimate" if level == "C5" else "none",
            "reference_audit": {"status": "pass", "reviewer": "codex_source_review", "human_approved": False,
                                "reason": "Each requirement is anchored in the specified original block(s)."}})
    out.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if out.exists() and out.read_text(encoding="utf-8") != content:
        raise ValueError("diagnostic set differs: write a new version instead")
    out.write_text(content, encoding="utf-8")
    print(json.dumps({"n": len(rows), "sha256": hashlib.sha256(content.encode()).hexdigest(), "path": str(out)}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    build(a.corpus, a.out)
