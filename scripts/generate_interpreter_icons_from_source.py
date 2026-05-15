#!/usr/bin/env python3
import sys
from pathlib import Path
import hashlib
import base64

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app, db, client, generate_resized_image
from app import Interpreter

OUT_DIR  = ROOT / "static" / "images" / "interpreters_hq"
OUT_DIR.mkdir(parents=True, exist_ok=True)

STYLE = "Create a new portrait matching the style of the input image. high quality anime style. "

ICON_SUBJECTS = {
  "storyteller": "A warm storyteller character portrait: think Morgan Freeman, elderly male, black skin, gentle smile, soft scarf or cardigan, cozy book tucked under arm, reassuring and grounded.",
  "psychoanalyst": "A classic psychoanalyst character portrait: think Freud, tidy hair, round spectacles, subtle blazer and tie, calm thoughtful expression, soft notebook or clipboard hint.",
  "wry": "A wry, amused character portrait: Think Jack Black, slight smirk, raised eyebrow, relaxed posture, simple casual jacket, playful but kind vibe.",
  "sage": "An ancient reflective sage portrait: Think Confucius, serene face, simple robe, small wooden staff or beads, quiet wisdom, minimal detail.",
  "anchor": "A folksy moral anchor portrait: Think Uncle Jesse, warm friendly face, denim/utility shirt hint, steady eyes, subtle badge/heart pin suggesting values and loyalty.",
  "optimist": "A wide-eyed optimist portrait: Think Elf, bright gentle smile, open expression, simple star or sun motif, uplifting and tender.",
  "humanist": "A curious analytical humanist portrait: Think Data (star trek), thoughtful gaze, sci-fi uniform, warm intelligence.",
  "philosopher": "A disciplined rational philosopher portrait: Think Spock, A calm logical sci-fi thinker with pointed ears and raised eyebrow, simplified and non-realistic. restraint.",
  "therapist": "A gentle practical therapist portrait: kind face, simple sweater, small clipboard or tea mug, supportive and approachable.",
  "bard": "A mythic bard portrait: inspired eyes, simple cloak, small lute/harp hint, heroic-story vibe without magic.",
  "detective": "A noir detective portrait: fedora silhouette, trench coat, subtle magnifying glass or streetlamp motif, cool but empathetic.",
  "skeptic": "A compassionate skeptic portrait: neutral calm face, simple hoodie/jacket, small checklist icon, grounded and realistic.",
  "stoic": "A stoic mentor portrait: steady eyes, minimal expression, simple laurel or column motif, calm strength.",
  "surreal": "A playful surrealist portrait:  Think Mystic / Psychic, quirky hair/hat, abstract shapes floating behind, curious grin, whimsical but kind.",
  "coach": "A protective coach portrait: confident smile, athletic jacket, subtle whistle or shield motif, encouraging and no-nonsense.",
  "seer": "A symbolic seer portrait: Think Tara Parker, bright red curly hair, calm mysterious eyes, scarf and crystal-orb silhouette, swirling symbol shapes—mystique without prophecy.",
  "rogue": "A charming rogue portrait: Think Charming Pirate, playful grin, bandana on head under a pirate hat, long dreadlicks, feathery earrings, mischievous but insightful.",
}


# Optional: custom per-icon subject overrides by slug/icon_key
SUBJECT_OVERRIDES = {
    # "spock_like": "A calm logical sci-fi thinker with pointed ears and raised eyebrow, simplified and non-realistic.",
}

def parse_list(s: str | None):
    if not s:
        return None
    return {x.strip() for x in s.split(",") if x.strip()}


def icon_subject(it: Interpreter) -> str:
    key = (it.icon_key or "").strip()
    if key and key in SUBJECT_OVERRIDES:
        return SUBJECT_OVERRIDES[key]
    if it.slug in SUBJECT_OVERRIDES:
        return SUBJECT_OVERRIDES[it.slug]
    if key in ICON_SUBJECTS:
        return ICON_SUBJECTS[key]
    return f"A friendly character portrait representing: {it.name}. Calm, approachable expression."



def main(force=False, slugs=None, keys=None):
    with app.app_context():
        rows = (Interpreter.query
                #.filter_by(is_enabled=True)
                .order_by(Interpreter.sort_order.asc(), Interpreter.id.asc())
                .all())

        slugs = set(slugs or [])
        keys = set(keys or [])
        
        for it in rows:
            if slugs and it.slug not in slugs:
                continue
            if keys and (it.icon_key or "").strip() not in keys:
                continue
        
        # for it in rows:
            subject = icon_subject(it)

            prompt = f"{STYLE}\n\nSubject: {subject}\n"

            # Small hash to change filename when prompt changes
            h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
            filename = f"{it.slug}-{h}.png"

            out_path  = OUT_DIR / filename

            # if not force and it.icon_file and out_path.exists():
            #     print(f"SKIP {it.slug}: already has icon_file={it.icon_file}")
            #     continue

            if not force and it.icon_file:
                print(f"SKIP {it.slug}: already has icon_file={it.icon_file}")
                continue


#            resp = client.images.generate(
#                model=MODEL,
#                prompt=prompt,
#                n=1,
#                size=SIZE,
#            )
 

            resp = client.images.edit(
                model="gpt-image-1",
                image=Path("/data/dreamr-frontend/static/images/interpreters/default.png"),
                prompt=prompt,
                size="1024x1024",
                input_fidelity="high",
                quality="high",
            )

            img_bytes = base64.b64decode(resp.data[0].b64_json)
            out_path.write_bytes(img_bytes)

            # DB update (matches your columns)
            #it.icon_file = filename
            #it.icon_prompt = prompt
            #db.session.commit()

            print(f"OK   {it.slug}: {filename}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="regenerate even if icon exists")
    p.add_argument("--slugs", help="comma-separated slugs, e.g. warm_storyteller,noir_detective")
    p.add_argument("--keys", help="comma-separated icon_keys, e.g. detective,sage")
    args = p.parse_args()

    main(
        force=args.force,
        slugs=parse_list(args.slugs),
        keys=parse_list(args.keys),
    )

