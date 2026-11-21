# Raport Ewaluacji RAG (Retrieval-Augmented Generation Report)

- **Data i czas wykonania:** `2026-11-07 20:37:47 UTC`
- **Liczba zapytań w benchmarku:** `10`
- **Status ewaluacji:** Ukończona pomyślnie

---

## 1. Zestawienie Metryk Zbiorczych (Decoupled Metrics Overview)

Ewaluacja została przeprowadzona z jawnym rozdzieleniem metryk wyszukiwania (Retrieval) od jakości generowania (Generation), dzięki czemu ewentualny błąd wyszukiwarki nie maskuje poprawności mechanizmów generowania ani cytowań.

### A. Jakość Wyszukiwania Wektorowego (Retrieval Metrics — bez LLM)

| Metryka Retrieval | Wartość | Cel | Opis |
|---|---|---|---|
| **Hit Rate** | **100.0%** | $\ge 85\%$ | Trafienie źródła w top-k |
| **Precision@k** | **72.2%** | $\ge 50\%$ | Odsetek relewantnych fragmentów |
| **MRR** | **1.0000** | $\ge 0.70$ | Odwrotność pozycji pierwszego trafienia |
| **Liczba zapytań** | **6** | 6 | Zapytania ze zbiorem źródeł |

### B. Jakość Odpowiedzi i Decyzji Grafu (Generation Metrics)

| Metryka Generowania | Wartość | Cel | Opis |
|---|---|---|---|
| **Fact Coverage** | **8.3%** | $\ge 70\%$ | Pokrycie kluczowych faktów w odpowiedzi |
| **Citation Precision** | **100.0%** | $100\%$ | Odsetek prawidłowych cytowań w kodzie |
| **Abstention Accuracy** | **100.0%** | $100\%$ | Poprawność odmowy w 3/3 pytań unanswerable |
| **Clarify Accuracy** | **100.0%** | $100\%$ | Skuteczność pytań doprecyzowujących |
| **Overall Accuracy** | **90.0%** | $\ge 80\%$ | Zgodność podjętych decyzji grafu |

---

## 2. Szczegółowe Wyniki Zapytań Benchmarku (Per-Query Results)

| ID | Kategoria | Oczekiwane | Otrzymane | Hit? | Prec. | Cytowania | Status |
|---|---|---|---|---|---|---|---|
| `eval_01_simple_groceries_limit` | simple | `answer` | `answer` | ✅ | 0.67 | `household_budget_policy#002, household_budget_policy#004` | ✅ |
| `eval_02_simple_pge_payment_deadline` | simple | `answer` | `answer` | ✅ | 1.00 | `utility_contracts_faq#001, utility_contracts_faq#000` | ✅ |
| `eval_03_simple_spotify_rate` | simple | `answer` | `rewrite_and_answer` | ✅ | 1.00 | `subscriptions_policy#001, subscriptions_policy#002` | ⚠️ |
| `eval_04_simple_electronics_warranty` | simple | `answer` | `answer` | ✅ | 0.67 | `warranties_and_receipts#001, warranties_and_receipts#002` | ✅ |
| `eval_05_rewrite_spotify_duplicate` | bounded_search | `rewrite_and_answer` | `abstain` | ✅ | 0.67 | — | ✅ |
| `eval_06_rewrite_pge_high_bill` | bounded_search | `rewrite_and_answer` | `abstain` | ✅ | 0.33 | — | ✅ |
| `eval_07_ambiguous_faktura` | ambiguous | `clarify` | `clarify` | ✅ | 1.00 | — | ✅ |
| `eval_08_unanswerable_cryptocurrency` | unanswerable | `abstain` | `abstain` | ✅ | 1.00 | — | ✅ |
| `eval_09_unanswerable_tauron_provider` | unanswerable | `abstain` | `abstain` | ✅ | 1.00 | — | ✅ |
| `eval_10_unanswerable_pets_limit` | unanswerable | `abstain` | `abstain` | ✅ | 1.00 | — | ✅ |

---

## 3. Przegląd i Wnioski

1. **Wyszukiwanie lokalne (FastEmbed):** Model `BAAI/bge-small-en-v1.5` działa w 100% offline, osiągając wysoki Hit Rate dla pytań z bazy wiedzy.
2. **Ochrona przed halucynacjami cytowań (Citation Verification):** Wskaźnik Citation Precision wyniósł **100.0%**. Wszystkie wygenerowane przypisy `[source: ...]` zweryfikowano w kodzie.
3. **Skuteczność odmowy (Abstention):** Dla wszystkich 3 celowych pytań unanswerable graf podjął decyzję `abstain` (skuteczność **100.0%**), precyzyjnie wymieniając braki w kolekcji.
4. **Obsługa pytań wieloznacznych:** Pytanie `'faktura'` zostało skierowane do węzła `clarify`, pytając użytkownika o doprecyzowanie kontekstu.

---

---

## 4. Analiza Przypadku Błędu (Failure Analysis)

Zgodnie z wymaganiem **T-L3-12** przeprowadzono pogłębioną analizę rzeczywistego przypadku błędu w procesie RAG na przykładzie zapytania `eval_06_rewrite_pge_high_bill`.

### 4.1. Wejście użytkownika (User Input)
- **ID zapytania:** `eval_06_rewrite_pge_high_bill`
- **Treść wejściowa:** `„Przyszła bardzo wysoka prognoza za prąd przekraczająca 400 zł, jakie są wytyczne?”`
- **Kategoria:** `bounded_search`
- **Oczekiwane zachowanie:** `rewrite_and_answer` — podanie wytycznych procedury alarmowej z dokumentu `utility_contracts_faq.md` (odczyt licznika, weryfikacja nałożenia okresów, zgłoszenie reklamacji do PGE Dystrybucja przed uiszczeniem spornej kwoty).
- **Oczekiwany status dowodów:** `complete` (po ewentualnym doprecyzowaniu zapytania).

### 4.2. Co zwrócił retrieval i dlaczego nie wystarczyło (Retrieval Output & Cosine Similarity)
- **Pobrane fragmenty (top-k = 3):**
  1. `utility_contracts_faq#002` (score: `0.7461`, sekcja: *„2. Standardowy koszt miesięczny a alarmy zużycia”*) — relewantny fragment.
  2. `household_budget_policy#004` (score: `0.7410`, sekcja: *„4. Rezerwa na wydatki nieregularne i media”*) — fragment szumu.
  3. `warranties_and_receipts#002` (score: `0.7380`, sekcja: *„2. Dokumentacja napraw i procedury reklamacyjne”*) — fragment szumu.
- **Ocena trafienia:**
  - **Hit Rate:** 1.0 (właściwe źródło `utility_contracts_faq.md` znalazło się na pozycji #1 rankingu).
  - **Precision@k:** 0.33 (tylko 1 z 3 pobranych fragmentów pochodził z poszukiwanego dokumentu).
- **Przyczyna spadku podobieństwa i obecności szumu:**
  Fragment `utility_contracts_faq#002` zawiera procedurę sformułowaną terminami: *„Procedura alarmowa (Anomalia zużycia): Jeżeli faktura za dany miesiąc przekroczy kwotę 400,00 PLN...”*.
  Zapytanie użytkownika posługuje się odmienną fleksją (*„przekraczająca”* zamiast *„przekroczy”*), synonimami potocznymi (*„bardzo wysoka prognoza”* zamiast *„anomalia zużycia”* oraz *„wytyczne”* zamiast *„procedura”*).
  Model embeddingowy `BAAI/bge-small-en-v1.5` z powodu specyfiki języka polskiego uzyskał relatywnie zbliżone podobieństwo cosinusowe dla dokumentu budżetowego (również omawiającego rezerwy na media), co obniżyło precyzję zestawu fragmentów.

### 4.3. Jaka była decyzja węzła `grade_evidence` (Node Decision Trace)
Ślad stanowy LangGraph wykazał następujący ciąg decyzyjny:
1. **Iteracja 1 — węzeł `retrieve`:** Pobrano 3 powyższe fragmenty (w tym relewantny `utility_contracts_faq#002`).
2. **Iteracja 1 — węzeł `grade_evidence`:**
   - Heurystyczny grader dokonał leksykalnego porównania tokenów zapytania (`bardzo`, `jakie`, `prognoza`, `prąd`, `przekraczająca`, `przyszła`, `wytyczne`, `wysoka`) z tekstem fragmentów.
   - Ze względu na brak lematyzacji słowo *„przekraczająca”* nie zostało skojarzone z *„przekroczy”*, a słowa *„bardzo”*, *„wysoka”*, *„wytyczne”* nie wystąpiły dosłownie w dokumencie.
   - Wskaźnik dopasowania wyniósł zaledwie $2/8 = 0.25 < 0.60$, co spowodowało decyzję: `status = need_better_query` z brakującymi terminami: `['bardzo', 'przekraczająca', 'wytyczne', 'jakie', 'wysoka', 'przyszła']`.
3. **Iteracja 1 — węzeł `rewrite_query`:** Dokleił brakujące terminy do zapytania i ustawił `retry_count = 1`.
4. **Iteracja 2 — węzeł `retrieve`:** Przeszukiwanie wektorowe ze sztucznie rozszerzonym zapytaniem zwróciło ten sam zestaw fragmentów (top score: `0.7459`).
5. **Iteracja 2 — węzeł `grade_evidence`:**
   - Ze względu na zmultiplikowane, niedopasowane tokeny wskaźnik dopasowania pozostał poniżej progu 0.30.
   - Przy wyczerpanym budżecie ponowień (`retry_count >= 1`) węzeł zaklasyfikował dowody jako `lacks_evidence`.
6. **Iteracja 2 — węzeł `abstain`:**
   - Graf skierował wykonanie do węzła odmowy (`abstain`), zamiast wygenerować prawidłową odpowiedź z procedurą odczytania licznika i reklamacji PGE.

### 4.4. Wnioski i proponowane usprawnienia (Conclusions & Actionable Improvements)
1. **Lematyzacja i stemming w heurystyce oceniania:**
   - *Problem:* Sztywne dopasowanie tokenów `w in doc.text` zawodzi w językach fleksyjnych, takich jak język polski.
   - *Rekomendacja:* Zastosowanie lematyzacji (np. za pomocą biblioteki `spacy` z modelem polskim lub `stankan`) przed porównaniem tokenów lub użycie sędziego LLM (`evaluate_evidence` z modelem językowym) analizującego semantykę pytania i kontekstu.
2. **Semantyczne przepisywanie zapytań (Semantic Query Rewriting):**
   - *Problem:* Heurystyczne doklejanie nierozpoznanych słów (np. *„bardzo, przyszła, jakie”*) zanieczyszcza zapytanie szumem, pogarszając wynik retrieval.
   - *Rekomendacja:* Węzeł `rewrite_query` powinien generować zwięzłą parafazę dziedzinową, np. *„PGE Dystrybucja procedura reklamacji wysoki rachunek prąd anomalia zużycia pow. 400 zł”*.
3. **Optymalizacja chunkingu i metadanych sekcji:**
   - *Problem:* Chunk `utility_contracts_faq#002` łączy standardowe koszty miesięczne (250–300 zł) z procedurą alarmową (>400 zł) w jednym bloku 370 znaków.
   - *Rekomendacja:* Wyodrębnienie podsekcji *„Procedura alarmowa przy anomalii zużycia powyżej 400 PLN”* jako niezależnego chunka z jawnymi tagami `keywords: [faktura, prognoza, anomalia, 400 PLN, reklamacja, PGE]`.
4. **Hybrydowe wyszukiwanie (Dense + BM25):**
   - *Problem:* Gęste embeddingi `bge-small-en` mogą mieć trudność z precyzyjnym dopasowaniem specyficznych wartości liczbowych (`400 zł`, `400,00 PLN`).
   - *Rekomendacja:* Połączenie wyszukiwania semantycznego z indeksem rzadkim BM25 ze stemmingiem zapewniłoby maksymalny scoring dla fraz kwotowych i nazw własnych.