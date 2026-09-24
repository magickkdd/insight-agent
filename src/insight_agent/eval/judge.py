"""LLM-as-judge：软性期望的判分器。

纪律（防 judge 抽风的三件套）：
  temperature=0 + 判据写死在提示词 + 只输出 PASS/FAIL JSON
judge 输出解析失败 → 判 FAIL 并注明（宁可误杀不可放过静默问题）。
"""

import json

from openai import OpenAI

JUDGE_PROMPT = """你是严格的评审。根据评判问题，判断被评内容是否合格。

判分要求：以评判问题为准，不脑补，不放宽标准。
只输出 JSON：{"verdict": "PASS" 或 "FAIL", "reason": "一句话理由"}"""


def _extract_json_object(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"回复里找不到 JSON 对象：{text[:200]}")
    return text[start : end + 1]


def run_judge(client: OpenAI, model: str, rubric: str, content: str) -> tuple[bool, str]:
    return run_judge_single(client, model, rubric, content, temperature=0.0)


def run_judge_single(
    client: OpenAI, model: str, rubric: str, content: str, *, temperature: float
) -> tuple[bool, str]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"评判问题：{rubric}\n\n被评内容：\n{content[:6000]}"},
        ],
        temperature=temperature,
    )
    raw = resp.choices[0].message.content or ""
    try:
        data = json.loads(_extract_json_object(raw))
        return data.get("verdict") == "PASS", f"judge={data.get('verdict')}：{data.get('reason', '')}"
    except (ValueError, json.JSONDecodeError) as e:
        return False, f"judge 输出无法解析（{e}）"


def run_judge_multi(
    client: OpenAI, model: str, rubric: str, content: str, *, votes: int = 3
) -> tuple[bool, str]:
    """多票制（规格 §3.2）：不同温度投 3 票，多数决；一致率不足标注低置信。"""
    temps = (0.0, 0.3, 0.7)[:votes]
    results, last_detail = [], ""
    for t in temps:
        ok, detail = run_judge_single(client, model, rubric, content, temperature=t)
        results.append(ok)
        last_detail = detail
    agrees = sum(results) / len(results)
    majority = sum(results) > len(results) / 2
    conf = "" if agrees == 1.0 else f"（⚠低置信 {agrees:.0%}）"
    return majority, f"{last_detail}{conf}｜多票 {sum(results)}/{len(results)}"
