"""Ground local Qwen selection one act at a time to bound visual context.

Each act configuration declares a contiguous candidate range and shot budget.
The model sees every real frame in one labeled image; its raw choices, order,
story prose, prompt and inference counters are saved without silent padding.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
from pathlib import Path
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quickcut.core import load_manifest
from quickcut.vlm import _open_ollama, _visual_payload


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest")
    p.add_argument("act_config")
    p.add_argument("--output", required=True)
    a = p.parse_args()
    manifest = load_manifest(a.manifest)
    config = json.loads(Path(a.act_config).read_text())
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    calls, beats, order = [], [], []
    for act in config["acts"]:
        from PIL import Image, ImageDraw, ImageFont
        pool = [s for s in manifest.shots if act["start"] <= s.start < act["end"]]
        ids = [s.shot_id for s in pool]
        images, slots, keyframes = _visual_payload(pool, require_visual=True, visual_mode="contact_sheet", contact_sheet_size=24)
        if len(images) != 1:
            raise ValueError("Each act must fit one contact sheet (24 frames maximum)")
        sheet = Image.new("RGB", (4 * 320, ((len(pool) + 3) // 4) * 220), "#171717")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 24)
        for i, shot in enumerate(pool):
            frame = Image.open(shot.keyframe).convert("RGB").resize((320, 180))
            x, y = (i % 4) * 320, (i // 4) * 220
            sheet.paste(frame, (x, y))
            draw.text((x + 10, y + 186), f"{i+1} | {shot.start:g}s", font=font, fill="white")
        buffer = io.BytesIO()
        sheet.save(buffer, format="JPEG", quality=94)
        sheet.save(out.with_name(f"qwen_{act['id']}_contact.jpg"), quality=94)
        images = [base64.b64encode(buffer.getvalue()).decode()]
        slots = [{"sha256": hashlib.sha256(buffer.getvalue()).hexdigest(),
                  "position_to_shot_id": {str(i+1): s.shot_id for i, s in enumerate(pool)}}]
        instruction = act['instruction']
        for i, shot in enumerate(pool):
            instruction = instruction.replace(shot.shot_id, f"编号{i+1}")
        prompt = (f"你是剪辑师。图中每格标有大号编号和源时间。{instruction}\n"
                  f"从这{len(ids)}个镜头里选恰好{act['count']}个，删除重复视角，保留关键动作，按时间从早到晚排序。"
                  "返回selected_positions整数数组，再用一句中文在description概括所选镜头中的动作。"
                  "必须看清谁抓住谁，不能颠倒角色。不要虚构追逐，不要写创作说明。"
                  "selected_positions只写图中所选镜头编号，必须从小到大严格升序，不要把必选镜头放在数组最前面。不能把全部候选照抄。忽略之前旁白字数要求，只返回一句概括。")
        schema = {"type": "object", "required": ["selected_positions", "description"],
                  "properties": {"selected_positions": {
                      "type": "array", "items": {"type": "integer", "enum": list(range(1, len(pool)+1))},
                      "minItems": act["count"], "maxItems": act["count"], "uniqueItems": True},
                      "description": {"type": "string", "maxLength": 100}}}
        payload = {"model": "qwen2.5vl:3b", "prompt": prompt, "images": images, "format": schema,
                   "stream": False, "options": {"num_ctx": 8192, "temperature": 0.1, "num_predict": 512}}
        req = Request("http://127.0.0.1:11434/api/generate", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        with _open_ollama(req, timeout=900, base_url="http://127.0.0.1:11434") as response:
            raw = json.load(response)
        out.with_name(f"qwen_{act['id']}_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
        result = json.loads(raw["response"])
        evidence = {"act": act, "request_prompt": prompt, "raw_payload": raw,
                    "model_plan": result, "image_slots": slots, "keyframes": keyframes}
        out.with_name(f"qwen_{act['id']}_inference.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        required = [ids.index(i) + 1 for i in act.get("required_ids", [])]
        if not set(required) <= set(result.get("selected_positions", [])):
            correction_prompt = (f"请纠正你之前的选镜头结果{result.get('selected_positions')}，其中缺少关键动作。"
                                 f"必须包括编号{required}，总数恰好{act['count']}个。其他编号请看图补充。"
                                 "按编号升序，只返回selected_positions和一句真实的description。不能用1到8这样的连续编号敷衍。")
            payload["prompt"] = correction_prompt
            req = Request("http://127.0.0.1:11434/api/generate", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
            with _open_ollama(req, timeout=900, base_url="http://127.0.0.1:11434") as response:
                correction_raw = json.load(response)
            result = json.loads(correction_raw["response"])
            evidence["anchor_correction"] = {"prompt": correction_prompt, "raw_payload": correction_raw, "model_plan": result}
            out.with_name(f"qwen_{act['id']}_inference.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        positions = result.get("selected_positions")
        if not isinstance(positions, list) or not positions or any(type(i) is not int or not 1 <= i <= len(pool) for i in positions):
            raise ValueError("Model positions must be integer indexes within the visual pool")
        selected = [ids[i-1] for i in positions]
        if len(selected) != act["count"] or len(set(selected)) != len(selected) or not set(selected) <= set(ids):
            raise ValueError(f"Invalid actual Qwen selection for {act['id']}: {selected}")
        if selected != sorted(selected, key=lambda i: next(s.start for s in pool if s.shot_id == i)):
            # Ask the local model to correct its own ordering; retain both
            # responses instead of passing an editorial sort off as Qwen's.
            repair_prompt = (f"你选出的镜头编号是{result['selected_positions']}。保留这些编号，一个不增一个不减。"
                             "请按数字从小到大排序，让故事按源时间发生。只返回selected_positions整数数组的JSON。")
            repair_schema = {"type": "object", "required": ["selected_positions"], "properties": {
                "selected_positions": {"type": "array", "items": {"type": "integer", "enum": result["selected_positions"]},
                                       "minItems": act["count"], "maxItems": act["count"]}}}
            repair = {"model": "qwen2.5vl:3b", "prompt": repair_prompt, "format": repair_schema,
                      "stream": False, "options": {"temperature": 0, "num_predict": 128}}
            req = Request("http://127.0.0.1:11434/api/generate", data=json.dumps(repair).encode(),
                          headers={"Content-Type": "application/json"})
            with _open_ollama(req, timeout=900, base_url="http://127.0.0.1:11434") as response:
                repair_raw = json.load(response)
            repaired = json.loads(repair_raw["response"])["selected_positions"]
            evidence["order_correction"] = {"prompt": repair_prompt, "raw_payload": repair_raw}
            out.with_name(f"qwen_{act['id']}_inference.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            if (not isinstance(repaired, list)
                    or any(type(i) is not int or not 1 <= i <= len(pool) for i in repaired)
                    or repaired != sorted(result["selected_positions"])):
                raise ValueError("Model failed to correct its own temporal order")
            selected = [ids[i-1] for i in repaired]
        missing = set(act.get("required_ids", [])) - set(selected)
        if missing:
            raise ValueError(f"Model omitted reviewed story anchors: {missing}")
        calls.append(evidence)
        beats.append({"id": act["id"], "label": {"hook": "开头", "journey": "中段", "close": "结尾"}[act["id"]],
                      "text": result["description"], "narration": result["description"],
                      "source": "source", "source_shot_ids": selected})
        order.extend(selected)
        print(act["id"], json.dumps(result, ensure_ascii=False), flush=True)
    story = {"theme": config.get("theme", "林间反击"), "planner": "ollama:qwen2.5vl:3b",
             "shot_selection": True, "shot_order": order, "selected_shot_ids": order, "beats": beats,
             "narration": "".join(b["narration"] for b in beats),
             "inference": {"strategy": "three independent single-image acts", "calls": calls,
                           "candidate_shot_ids": [k["shot_id"] for c in calls for k in c["keyframes"]],
                           "grounded_shot_ids": [k["shot_id"] for c in calls for k in c["keyframes"]],
                           "normalization_changed": False}}
    out.write_text(json.dumps(story, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
