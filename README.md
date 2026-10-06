# Aniword History Extractor

> only tested on windows

a simple script to extract the history in csv and json format from Aniworld 




## Installation

after cloning:

### Windows:

```properties
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Linux:

```properties
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

<details>
<summary>Alternative: uv</summary>

### UV - Platform unabhängig 

> install uv here: <https://docs.astral.sh/uv/getting-started/installation/s>

```properties
uv sync
uv run export_verlauf.py
```

</details>



## Usage

```properties
python export_verlauf.py
```

- login credentials are stored in `.env` after the first run
- the Results are stored in the `output/` folder