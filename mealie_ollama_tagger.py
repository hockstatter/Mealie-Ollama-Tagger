#!/usr/bin/env python3
"""
Hybrid tagger for Mealie: rules for things that are checkable, a local Ollama
model (with schema-constrained output) for things that need judgment.

  LLM   -> course (exactly one) + cuisine (0-2)
  Rules -> method tags, dietary tags (not on desserts), Quick (from times)

SAFE BY DEFAULT:
  python mealie_ollama_tagger_v3.py                # dry-run
  python mealie_ollama_tagger_v3.py --apply        # create missing tags + apply

Environment: MEALIE_URL, MEALIE_TOKEN, OLLAMA_URL, OLLAMA_MODEL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests

COURSES = ["Breakfast", "Brunch", "Lunch", "Dinner", "Dessert", "Appetizer",
           "Side Dish", "Snack", "Drink", "Sauce", "Soup", "Salad", "Bread"]
CUISINES = ["American", "Italian", "Mexican", "Chinese", "Japanese", "Thai",
            "Indian", "Mediterranean", "French", "Greek", "Korean", "Tex-Mex"]

SYSTEM_PROMPT = f"""You classify recipes for a personal recipe library.

Fill in the JSON fields in order:
1. dish_type: a short phrase naming what the dish actually is (e.g. "baked cheesecake",
   "creamy seafood soup", "sheet-pan chicken and vegetables").
2. course: exactly ONE value from this list: {", ".join(COURSES)}.
3. cuisines: 0-2 values from this list: {", ".join(CUISINES)}.

Course guide:
- Dessert: sweet dishes - cakes, pies, cheesecakes, cookies, brownies, ice cream, candy.
- Breakfast: morning dishes - eggs, pancakes, waffles, French toast, oatmeal, breakfast casseroles.
- Brunch: only if it is clearly a brunch dish and not plain breakfast.
- Dinner: savory main dishes (meat, fish, pasta, casseroles, curries, stir-fries).
- Soup: soups, bisques, chowders, stews, chili.
- Salad / Bread / Drink / Sauce / Appetizer / Side Dish / Snack / Lunch: use when it clearly fits.
- A sweet baked dish is NEVER Dinner. A savory dish is NEVER Dessert.

Cuisine guide: choose a cuisine only if the dish is distinctly associated with it
(ingredients, technique, name). Generic cakes, cookies, casseroles etc. get an empty list.

Examples:
Title: Chocolate Lava Cake; ingredients: dark chocolate, butter, eggs, sugar, flour
-> {{"dish_type":"baked chocolate cake","course":"Dessert","cuisines":[]}}
Title: Chicken Tikka Masala; ingredients: chicken, garam masala, tomato, cream
-> {{"dish_type":"chicken curry","course":"Dinner","cuisines":["Indian"]}}
Title: Lobster Bisque; ingredients: lobster, cream, sherry, tomato paste
-> {{"dish_type":"creamy seafood soup","course":"Soup","cuisines":["French"]}}
Title: Blueberry Pancakes; ingredients: flour, milk, egg, blueberries
-> {{"dish_type":"pancakes","course":"Breakfast","cuisines":["American"]}}

Return ONLY the JSON object."""

SCHEMA = {
    "type": "object",
    "properties": {
        "dish_type": {"type": "string"},
        "course": {"type": "string", "enum": COURSES},
        "cuisines": {"type": "array", "maxItems": 2,
                     "items": {"type": "string", "enum": CUISINES}},
    },
    "required": ["dish_type", "course", "cuisines"],
}


# --------------------------------------------------------------------------
# Recipe text helpers
# --------------------------------------------------------------------------
def ingredient_lines(recipe: dict) -> list[str]:
    out = []
    for item in recipe.get("recipeIngredient") or []:
        if isinstance(item, dict):
            text = (item.get("display") or item.get("originalText") or
                    ((item.get("food") or {}).get("name")) or "")
            if item.get("note") and item["note"] not in text:
                text += " " + item["note"]
        else:
            text = str(item)
        if text.strip():
            out.append(text.strip())
    return out


def instruction_lines(recipe: dict) -> list[str]:
    out = []
    for step in recipe.get("recipeInstructions") or []:
        text = step.get("text", "") if isinstance(step, dict) else str(step)
        if text.strip():
            out.append(text.strip())
    return out


def minutes(value) -> int:
    """Parse '1 Hour 30 Minutes', '45 minutes', 'PT1H30M' -> minutes."""
    if not value:
        return 0
    s = str(value).lower()
    h = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    m = re.search(r"(\d+)\s*m", s)
    return int(float(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def total_minutes(recipe: dict) -> int:
    t = minutes(recipe.get("totalTime"))
    if t:
        return t
    return minutes(recipe.get("prepTime")) + minutes(recipe.get("performTime") or recipe.get("cookTime"))


# --------------------------------------------------------------------------
# Rule-based tags
# --------------------------------------------------------------------------
def rx(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


MEAT_FISH = rx([
    "beef", "pork", "bacon", "ham", "sausages?", "chicken", "turkey", "lamb", "veal",
    "steaks?", "pepperoni", "salami", "prosciutto", "pancetta", "duck", "venison",
    "brisket", "short ribs?", "spare ribs?", "baby back ribs?", "chorizo", "hot dogs?",
    "meatballs?", "lard", "gelatin", "fish", "salmon", "tuna", "shrimp", "prawns?",
    "crab", "crabmeat", "lobster", "cod", "tilapia", "scallops?", "clams?", "mussels?",
    "oysters?", "anchov(?:y|ies)", "sardines?", "crawfish", "squid", "calamari",
    "worcestershire", "bone broth",
])
DAIRY_EXC = re.compile(
    r"peanut butter|almond butter|cashew butter|sunflower(?: seed)? butter|nut butter|"
    r"cocoa butter|shea butter|butternut|butter beans?|butter lettuce|coconut (?:milk|cream)|"
    r"cream of coconut|almond milk|oat milk|soy ?milk|rice milk|cashew milk|"
    r"plant[- ]based milk|non[- ]?dairy|dairy[- ]free|vegan butter|cream of tartar", re.I)
DAIRY = rx([
    "milk", "cream", "butter", "buttermilk", "cheeses?", "cheddar", "mozzarella",
    "parmesan", "ricotta", "mascarpone", "feta", "yogh?urt", "whey", "casein", "ghee",
    "half[- ]and[- ]half", "custard", "queso", "paneer", "gouda", "provolone",
    "gruy[eè]re", "brie", "kefir", "milk chocolate", "white chocolate",
])
EGGS_ETC = rx(["eggs?", "mayo", "mayonnaise", "meringue", "honey"])
GLUTEN_EXC = re.compile(
    r"gluten[- ]free(?: \w+){0,3} (?:flour|pasta|bread|noodles?|bread ?crumbs|soy sauce|"
    r"tortillas?|crackers?|oats|panko|blend)|gluten[- ]free|almond flour|coconut flour|"
    r"rice flour|cassava flour|chickpea flour|tapioca flour|corn ?starch|cornmeal|"
    r"corn tortillas?|rice noodles?|tamari|flourless", re.I)
GLUTEN = rx([
    "flour", "wheat", "breads?", "bread ?crumbs?", "panko", "pasta", "spaghetti",
    "linguine", "fettuccine", "penne", "macaroni", "lasagna", "noodles?", "orzo",
    "couscous", "barley", "rye", "semolina", "farro", "bulgur", "seitan", "malt",
    "tortillas?", "crackers?", "cookies?", "graham", "oreos?", "ladyfingers?",
    "biscuits?", "buns?", "rolls?", "pita", "naan", "croutons?", "pie crusts?",
    "puff pastry", "phyllo", "pizza dough", "cake mix", "brownie mix", "soy sauce",
    "beer", "bisquick", "oats?", "cereal", "pretzels?", "wafers?",
])

METHODS = [  # (regex over title+description+instructions, tags to emit)
    (rx(["slow[- ]cooker", "crock[- ]?pot"]), ["Slow Cooker", "Crockpot"]),
    (rx(["instant pot", "pressure cook(?:er|ing)?", "instapot"]), ["Instant Pot", "Pressure Cooker"]),
    (rx(["air[- ]fryer", "air[- ]fry(?:ing)?"]), ["Air Fryer"]),
    (rx(["grill(?:ed|ing)?", "barbecue", "bbq"]), ["Grilling"]),
    (rx(["smoker", "pellet grill", "smoke at"]), ["Smoking"]),
    (rx(["one[- ]pot", "one[- ]pan", "one[- ]skillet"]), ["One-Pot"]),
    (rx(["sheet[- ]pan"]), ["Sheet Pan"]),
    (rx(["no[- ]bake"]), ["No-Bake"]),
]
OVEN = re.compile(r"\bpreheat\b|\bbake[ds]?\b|\bin (?:the|a) (?:preheated )?oven\b", re.I)


def strip(rx_: re.Pattern, text: str) -> str:
    return rx_.sub(" ", text)


def rule_tags(recipe: dict, course: list[str]) -> list[str]:
    ings = ingredient_lines(recipe)
    steps = instruction_lines(recipe)
    ing_text = "\n".join(ings)
    title = recipe.get("name", "") or ""
    desc = recipe.get("description", "") or ""
    method_text = "\n".join([title, desc, *steps])
    tags: list[str] = []

    # Method
    for pattern, names in METHODS:
        if pattern.search(method_text):
            tags += names
    if OVEN.search("\n".join(steps)):
        # A baked step beats a stray "no-bake" mention (e.g. a no-bake filling layer).
        tags = [t for t in tags if t != "No-Bake"]
        tags.append("Baking")

    # Time
    t = total_minutes(recipe)
    if 0 < t <= 30:
        tags.append("Quick")

    # Dietary - only with a reasonably complete ingredient list
    if len(ings) >= 3:
        has_meat = bool(MEAT_FISH.search(ing_text))
        dairy_text = DAIRY_EXC.sub(" ", ing_text)
        has_dairy = bool(DAIRY.search(dairy_text))
        has_egg_honey = bool(EGGS_ETC.search(ing_text))
        has_gluten = bool(GLUTEN.search(GLUTEN_EXC.sub(" ", ing_text)))
        if not has_meat and "Dessert" not in course:
            tags.append("Vegetarian")
            if not has_dairy and not has_egg_honey:
                tags.append("Vegan")
        if not has_dairy:
            tags.append("Dairy-Free")
        if not has_gluten:
            tags.append("Gluten-Free")
    return tags


# --------------------------------------------------------------------------
# LLM (course + cuisine)
# --------------------------------------------------------------------------
def ollama_classify(ollama_url: str, model: str, recipe: dict, timeout: int):
    ings = ingredient_lines(recipe)[:40]
    steps = " ".join(instruction_lines(recipe))[:1500]
    categories = [c.get("name", "") for c in (recipe.get("recipeCategory") or [])
                  if isinstance(c, dict)]
    user = (
        f"Title: {recipe.get('name', '')}\n"
        f"Description: {(recipe.get('description') or '')[:400]}\n"
        f"Existing categories: {', '.join(categories) or '(none)'}\n"
        f"Ingredients: {'; '.join(ings)}\n"
        f"Instructions: {steps}"
    )
    last_err = None
    for _ in range(2):
        try:
            r = requests.post(
                ollama_url.rstrip("/") + "/api/chat",
                json={
                    "model": model, "stream": False, "think": False,
                    "keep_alive": "10m", "format": SCHEMA,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": user}],
                    "options": {"temperature": 0, "num_predict": 200, "num_ctx": 4096},
                },
                timeout=timeout,
            )
            r.raise_for_status()
            data = json.loads(r.json()["message"]["content"])
            course = data.get("course")
            cuisines = [c for c in data.get("cuisines", []) if c in CUISINES]
            return (data.get("dish_type", ""),
                    [course] if course in COURSES else [], cuisines)
        except (ValueError, KeyError) as exc:
            last_err = exc
    raise RuntimeError(f"Bad Ollama output: {last_err}")


# --------------------------------------------------------------------------
# Mealie plumbing
# --------------------------------------------------------------------------
class Mealie:
    def __init__(self, base_url: str, token: str, timeout: int = 45):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/json",
                               "Content-Type": "application/json"})
        self.timeout = timeout

    def get(self, path, **kw):
        r = self.s.get(self.base + path, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, payload):
        r = self.s.post(self.base + path, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r

    def patch(self, path, payload):
        r = self.s.patch(self.base + path, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r


def get_all_recipes(api: Mealie, page_size: int) -> list[dict]:
    recipes, page = [], 1
    while True:
        data = api.get("/api/recipes", params={"page": page, "perPage": page_size})
        items = data.get("items", []) if isinstance(data, dict) else []
        recipes.extend(items)
        total = data.get("totalPages", page) if isinstance(data, dict) else page
        if not items or page >= int(total or page):
            break
        page += 1
    return recipes


def tag_items(data) -> list[dict]:
    items = data.get("items", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected tag-list response: {data!r}")
    return items


def get_existing_tags(api: Mealie) -> dict[str, dict]:
    return {str(t["name"]).casefold(): t for t in tag_items(api.get("/api/organizers/tags"))
            if t.get("name")}


def ensure_tag(api: Mealie, existing: dict[str, dict], name: str) -> dict:
    key = name.casefold()
    if key in existing:
        return existing[key]
    created = api.post("/api/organizers/tags", {"name": name})
    try:
        tag = created.json()
    except ValueError:
        tag = {}
    if not tag.get("id"):
        tag = next((t for t in tag_items(api.get("/api/organizers/tags"))
                    if t.get("name", "").casefold() == key), None)
    if not tag:
        raise RuntimeError(f"Created tag '{name}' but could not retrieve it.")
    existing[key] = tag
    return tag


def main():
    p = argparse.ArgumentParser(description="Hybrid rules + Ollama tagger for Mealie.")
    p.add_argument("--apply", action="store_true", help="Create missing tags and update recipes.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3:4b"))
    p.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://localhost:11434"))
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--pause", type=float, default=0.2)
    p.add_argument("--tag-list", help="Text file, one tag per line. Only these tags will ever be applied.")
    p.add_argument("--verbose", action="store_true", help="Show the model's dish_type guess.")
    args = p.parse_args()

    mealie_url, token = os.getenv("MEALIE_URL"), os.getenv("MEALIE_TOKEN")
    if not mealie_url or not token:
        sys.exit("Set MEALIE_URL and MEALIE_TOKEN environment variables first.")

    allow = None
    if args.tag_list:
        with open(args.tag_list, encoding="utf-8") as f:
            allow = {l.strip().casefold() for l in f
                     if l.strip() and not l.lstrip().startswith("#")}

    api = Mealie(mealie_url, token)
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN (no Mealie changes)'}")
    print(f"Mealie: {mealie_url}\nOllama: {args.ollama_url} | model: {args.model}")
    api.get("/api/app/about")
    requests.get(args.ollama_url.rstrip("/") + "/api/tags", timeout=15).raise_for_status()

    summaries = get_all_recipes(api, args.page_size)
    if args.limit:
        summaries = summaries[:args.limit]
    existing_tags = get_existing_tags(api)
    print(f"Found {len(summaries)} recipes and {len(existing_tags)} existing tags.\n")

    changed = skipped = errors = 0
    n = len(summaries)
    for i, summary in enumerate(summaries, 1):
        slug = summary.get("slug")
        name = summary.get("name") or slug or f"Recipe {i}"
        if not slug:
            print(f"[{i}/{n}] SKIP: no slug")
            skipped += 1
            continue
        try:
            recipe = api.get(f"/api/recipes/{slug}")
            current = recipe.get("tags") or []
            have = {t.get("name", "").casefold() for t in current}

            dish_type, course, cuisines = ollama_classify(
                args.ollama_url, args.model, recipe, args.timeout)
            proposed = [(t, "llm") for t in course + cuisines]
            proposed += [(t, "rule") for t in rule_tags(recipe, course)]

            seen, additions = set(), []
            for tag, src in proposed:
                k = tag.casefold()
                if k in seen or k in have or (allow is not None and k not in allow):
                    continue
                seen.add(k)
                additions.append((tag, src))

            if not additions:
                print(f"[{i}/{n}] {name}: no new tags")
                skipped += 1
                continue

            print(f"[{i}/{n}] {name}")
            if args.verbose:
                print(f"    Model sees: {dish_type}")
            print(f"    Existing: {', '.join(t.get('name', '') for t in current) or '(none)'}")
            print(f"    Add:      {', '.join(f'{t} [{s}]' for t, s in additions)}")

            if args.apply:
                merged = list(current)
                for tag_name, _ in additions:
                    tag = ensure_tag(api, existing_tags, tag_name)
                    merged.append({"id": tag["id"], "name": tag["name"],
                                   "slug": tag.get("slug", ""),
                                   "groupId": tag.get("groupId") or tag.get("group_id")})
                api.patch(f"/api/recipes/{slug}", {"tags": merged})
                print("    APPLIED")
                changed += 1
        except Exception as exc:
            errors += 1
            print(f"[{i}/{n}] ERROR {name}: {exc}", file=sys.stderr)
        if args.pause:
            time.sleep(args.pause)

    print("\nFinished.")
    print(f"Recipes updated: {changed if args.apply else '(dry-run; none)'}")
    print(f"Skipped/no new tags: {skipped}\nErrors: {errors}")
    if not args.apply:
        print("\nNothing was changed. Review the suggestions above, then rerun with --apply.")


if __name__ == "__main__":
    main()
