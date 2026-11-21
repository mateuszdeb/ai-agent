# Punkt odniesienia UI: T-01

Data pomiaru: 2026-10-25. Zakres: wyłącznie ustalenie baseline i przygotowanie współdzielonych danych testowych przed implementacją backendu przeglądarkowego.

## Stan przed zmianami T-01

Repozytorium zawierało wcześniejsze, niezatwierdzone w Git zmiany narzędzi, integracji MCP i dokumentacji. Nie czyszczono ich, nie wykonywano resetu, checkoutu, commitu ani synchronizacji z inną kopią kodu.

Stan `git status --short` przed rozpoczęciem:

```text
 M .DS_Store
 M .gitignore
 M README.md
 M pyproject.toml
 M src/agent_project/agent.py
 M src/agent_project/tools.py
 M tests/test_tools.py
 M uv.lock
?? PLAN.md
?? design.md
?? evidence/trace_multitool_adaptation.jsonl
?? evidence/trace_multitool_adaptation_attempt_20261025_002216_103533.jsonl
?? requirements.md
?? src/agent_project/multitool_demo.py
?? src/agent_project/mcp_server.py
?? src/agent_project/models.py
?? tasks.md
?? tests/conftest.py
?? tests/test_agent_adaptation.py
?? tests/test_controlled_actions.py
?? tests/test_multitool_demo.py
?? tests/test_mcp_server.py
?? tests/test_tool_validation.py
?? workflow.md
```

Środowisko pomiaru: istniejące środowisko projektu, Python 3.12.15, pytest 9.1.1, Ruff 0.16.7. Nie instalowano ani nie synchronizowano zależności.

## Zmierzony baseline

```text
UV_CACHE_DIR=/tmp/ai-agent-uv-cache UV_OFFLINE=1 uv run --no-sync pytest -ra
147 passed in 1.95s

UV_CACHE_DIR=/tmp/ai-agent-uv-cache UV_OFFLINE=1 uv run --no-sync ruff check .
All checks passed!
```

To wynik wykonany przed zmianami T-01, nie przepisany z wcześniejszej dokumentacji. Nie uruchamiano żywego modelu ani demonstracji wielonarzędziowej.

## Chronione pliki

| Plik | SHA-256 przed T-01 |
| --- | --- |
| `data/transactions.json` | `83f372f6c4ca83e4850984f5af6d676df18ce9933842ed7f7ef1142a41129671` |
| `evidence/trace_multitool_adaptation.jsonl` | `d218d57fddf97b1f09a75b9cac2d9e754d164d3a70e647bd3331eae520207275` |

Kontrola po zmianach musi potwierdzić oba hashe. Istniejący dowód sukcesu nie jest regenerowany ani nadpisywany.

## Współdzielone fixture

- **`controlled_dataset`:** Zachowana istniejąca fixture pięciu stałych transakcji, z bazowym JSON w `tmp_path` i początkowo nieistniejącym ledgerem. Teardown sprawdza hash tymczasowego baseline.
- **`controlled_clock`:** Nowy zegar zaczynający od `2026-10-25T00:00:00+00:00`. Metody `now()`, `monotonic()` i `advance(seconds)` umożliwiają testowanie TTL bez `sleep` i bez zmiany globalnego czasu.
- **`offline_model_factory`:** Nowa fabryka klientów opartych na istniejącym `MockClient`. Każdy klient ma niezależną kolejkę odpowiedzi, provider/model `mock` oraz snapshoty argumentów w `calls`; wyczerpanie kolejki jest błędem, a nie automatyczną odpowiedzią.

Przykładowe użycie przez przyszły test:

```python
def test_future_ttl(controlled_clock, offline_model_factory, controlled_dataset):
    data_path, changes_path = controlled_dataset
    client = offline_model_factory([{"text": "Ready", "tool_calls": []}])
    controlled_clock.advance(600)
    assert controlled_clock.monotonic() == 600
    assert client.complete([])["text"] == "Ready"
    assert data_path.exists()
    assert not changes_path.exists()
```

To przykład użycia narzędzi testowych, nie implementacja sesji, TTL decyzji ani endpointu UI. Właściwe serwisy otrzymają zegar i klienta przez jawne zależności w późniejszych zadaniach.

## Wynik po zmianach

```text
UV_CACHE_DIR=/tmp/ai-agent-uv-cache UV_OFFLINE=1 uv run --no-sync pytest tests/test_ui_test_support.py -ra
12 passed in 0.06s

UV_CACHE_DIR=/tmp/ai-agent-uv-cache UV_OFFLINE=1 uv run --no-sync pytest -ra
159 passed in 1.41s

UV_CACHE_DIR=/tmp/ai-agent-uv-cache UV_OFFLINE=1 uv run --no-sync ruff check .
All checks passed!
```

Po testach ponownie obliczono SHA-256 obu chronionych plików; wyniki są identyczne z tabelą powyżej. Nowe testy zapisują wyłącznie do ścieżek tymczasowych i sprawdzają: kontrolowany czas, odrzucanie nieprawidłowego przesunięcia, izolację danych i ledgera, niezależne kolejki i snapshoty klienta oraz współpracę fixture z istniejącą pętlą agenta.

Zakres zmian T-01:

- `tests/conftest.py`: zachowana fixture transakcji; dodane kontrolowany zegar i fabryka klientów offline.
- `tests/test_ui_test_support.py`: 12 nowych przypadków testowych, wliczając parametryzację.
- `docs/ui-baseline.md`: niniejszy raport.
- `tasks.md`: odnotowane wykonanie wyłącznie T-01.

Nie zmieniano kodu produkcyjnego, konfiguracji zależności, wymagań, projektu technicznego, README ani planu narzędzi i integracji MCP (`PLAN.md`). Nie uruchamiano serwera UI, żywego modelu ani demonstracji. T-01 jest ukończone; T-02 pozostaje nierozpoczęte.
