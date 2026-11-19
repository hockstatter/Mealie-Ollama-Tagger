# Mealie Ollama Tagger

Automatically tag the recipes in your [Mealie](https://mealie.io) library using a local [Ollama](https://ollama.com) model plus some simple rules. Nothing leaves your network.

The script is **dry-run by default**. It prints what it *would* add, and only changes Mealie when you pass `--apply`.

## How it works

Tagging is split by what each method is good at:

| Tags | Source | How |
|---|---|---|
| Course (Breakfast, Dinner, Dessert, Soup, ...) | Ollama | Exactly one course, chosen from a fixed list |
| Cuisine (Italian, Mexican, ...) | Ollama | 0-2 cuisines, only when the dish clearly belongs to one |
| Method (Slow Cooker, Air Fryer, Baking, ...) | Rules | Keyword matches on the instructions |
| Dietary (Vegetarian, Vegan, Gluten-Free, Dairy-Free) | Rules | Ingredient-list checks (skipped for Dessert) |
| Quick | Rules | Total time of 30 minutes or less |

The model's output is constrained to a JSON schema, so it can't invent tags that aren't in the allowed lists.

The script only ever **adds** tags. It never removes or changes a recipe's existing tags, and it skips tags a recipe already has. Missing tags are created in Mealie on `--apply`.

Each suggestion in the output is labeled `[llm]` or `[rule]` so you can see where it came from.

## Requirements

- Python 3.9+
- A running Mealie instance and an API token
- [Ollama](https://ollama.com), recent enough to support structured outputs and the `think` option (update it if you get errors)

## Setup

### 1. Create the project folder

```bash
mkdir -p ~/scripts/mealie-tagger
cd ~/scripts/mealie-tagger
# Put mealie_ollama_tagger_v3.py here
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install requests
```

### 3. Pull the model

```bash
ollama pull qwen3:4b
```

`qwen3:4b` is the default and works well for this narrow task. If you have the VRAM, a larger model will be more accurate:

```bash
ollama pull qwen3:8b            # or: qwen2.5:7b-instruct
```

Then select it with `--model qwen3:8b` or `export OLLAMA_MODEL="qwen3:8b"`.

Avoid very small models (around 1.7B). They tended to put Dinner on desserts and Dessert on soups.

### 4. Get a Mealie API token

In Mealie, open your user profile, go to **API Tokens**, create a token, and copy it.

### 5. Set environment variables

```bash
export MEALIE_URL="http://YOUR-MEALIE-ADDRESS"
read -rsp "Mealie API token: " MEALIE_TOKEN; echo
export MEALIE_TOKEN

# If Ollama is on the same machine:
export OLLAMA_URL="http://localhost:11434"

# If Ollama is on your homelab server but you're running
# this script from another machine, use its LAN address instead,
# e.g. export OLLAMA_URL="http://192.168.1.50:11434"
```

The `read -rsp` line keeps the token out of your shell history and off the screen.

If Ollama is on another machine, it must be listening on the network (it binds to localhost by default). Set `OLLAMA_HOST=0.0.0.0` in the Ollama service environment and restart it. Only do this on a trusted LAN, since Ollama has no authentication.

## Usage

Run these from the project folder with the virtual environment active (`source .venv/bin/activate`).

**1. Dry run on a few recipes (changes nothing):**

```bash
python mealie_ollama_tagger_v3.py --limit 10 --verbose
```

`--verbose` shows what the model thinks each dish is, which makes wrong courses easy to diagnose.

**2. Apply to a small batch and check the result in Mealie:**

```bash
python mealie_ollama_tagger_v3.py --limit 10 --apply
```

**3. Run the whole library:**

```bash
python mealie_ollama_tagger_v3.py --apply
```

Sample output:

```
[2/10] The Best Chocolate Cake Recipe
    Model sees: baked chocolate cake
    Existing: (none)
    Add:      Dessert [llm], Baking [rule]
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--apply` | off | Create missing tags and update recipes. Without it, dry-run only. |
| `--limit N` | 0 (all) | Only process the first N recipes. |
| `--model NAME` | `qwen3:4b` | Ollama model. Also settable via `OLLAMA_MODEL`. |
| `--ollama-url URL` | `http://localhost:11434` | Also settable via `OLLAMA_URL`. |
| `--tag-list FILE` | none | Text file with one tag per line. **Only** these tags will ever be applied. |
| `--verbose` | off | Print the model's `dish_type` guess for each recipe. |
| `--page-size N` | 100 | Recipes fetched per Mealie API page. |
| `--timeout SECONDS` | 300 | Timeout for each Ollama request. |
| `--pause SECONDS` | 0.2 | Delay between recipes. |

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `MEALIE_URL` | yes | Base URL of your Mealie instance |
| `MEALIE_TOKEN` | yes | Mealie API token |
| `OLLAMA_URL` | no | Defaults to `http://localhost:11434` |
| `OLLAMA_MODEL` | no | Defaults to `qwen3:4b` |

## Restricting which tags are used

`--tag-list` is a filter. Create a file such as `my_tags.txt`:

```
# One tag per line; lines starting with # are ignored
Dessert
Dinner
Breakfast
Soup
Baking
Slow Cooker
```

```bash
python mealie_ollama_tagger_v3.py --tag-list my_tags.txt --limit 10
```

Any suggestion not in the file is dropped. Names must match the script's tag names (case-insensitive).

## Customizing

Edit the constants near the top of `mealie_ollama_tagger_v3.py`:

- `COURSES` and `CUISINES` set what the model can choose from.
- `METHODS` maps keyword patterns to method tags.
- The `MEAT_FISH`, `DAIRY`, `GLUTEN` and related patterns drive the dietary tags.

If you add a course or cuisine, the schema and prompt pick it up automatically.

## Limitations

- **Dietary tags are conservative guesses.** They check ingredient text only, so hidden sources (for example "stock" or "seasoning blend") can slip through. They err toward not tagging, but treat them as "likely" and don't rely on them for allergies.
- **Dietary tags are skipped for desserts**, to avoid tagging every cake as Vegetarian.
- **Cuisine is left empty** for generic dishes such as cakes, cookies and casseroles.
- **Method tags are keyword-based.** A recipe that mentions an oven in passing can pick up Baking, and a baked step overrides any "no-bake" mention.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Set MEALIE_URL and MEALIE_TOKEN environment variables first.` | Re-export the variables. They don't persist across new terminals. |
| `401 Unauthorized` from Mealie | The token is wrong or expired. Create a new one. |
| Connection refused to Ollama | Check `OLLAMA_URL`. If Ollama is on another machine, make sure it listens on the network. |
| `model not found` | Run `ollama pull <model>` for the model you selected. |
| Errors about `format` or `think` | Update Ollama to a recent version. |
| Slow runs | Use a smaller model, or confirm Ollama is using your GPU (`ollama ps`). |
| `Bad Ollama output` on some recipes | The script retries once. Persistent failures usually mean the model is too small. Try a larger one. |
| A tag was applied that you don't want | Remove it in Mealie, and use `--tag-list` to exclude it from future runs. |
