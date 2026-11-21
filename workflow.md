# Workflow tworzenia specyfikacji i implementacji

Każda nowa funkcja lub poprawka błędu powinna mieć specyfikację, która prowadzi od opisu potrzeby do wykonywalnego planu implementacji. Praca przebiega w trzech fazach: wymagania lub analiza błędu, projekt techniczny oraz zadania.

## Podstawowa struktura

Każda specyfikacja składa się z trzech kluczowych plików. Pierwszy plik zależy od rodzaju zmiany, a dwa pozostałe są wspólne dla obu ścieżek.

| Plik | Przeznaczenie |
| --- | --- |
| `requirements.md` | Specyfikacja funkcji: historyjki użytkownika i kryteria akceptacji zapisane w ustrukturyzowanej formie. |
| `bugfix.md` | Specyfikacja poprawki: analiza błędu oraz opis zachowania obecnego, oczekiwanego i pozostającego bez zmian. Zastępuje `requirements.md` w specyfikacji poprawki błędu. |
| `design.md` | Architektura techniczna, projekt komponentów, diagramy sekwencji, przepływ danych i uwarunkowania implementacyjne. |
| `tasks.md` | Szczegółowy plan implementacji podzielony na odrębne, wykonywalne zadania z możliwością śledzenia postępu. |

Przykładowa organizacja plików dla pojedynczej specyfikacji:

```text
specs/
└── <nazwa-zmiany>/
    ├── requirements.md  # albo bugfix.md
    ├── design.md
    └── tasks.md
```

## Wymagania lub analiza błędu

Celem pierwszej fazy jest określenie, co należy zbudować lub naprawić. Jej wynikiem jest `requirements.md` dla funkcji albo `bugfix.md` dla poprawki błędu.

### Specyfikacja funkcji: requirements.md

- **Cel i zakres:** Opisz potrzebę użytkownika, oczekiwany rezultat oraz elementy wyłączone z zakresu.
- **Historyjki użytkownika:** Stosuj zapis „Jako [rola] chcę [możliwość], aby [korzyść]”.
- **Kryteria akceptacji:** Zapisuj jednoznaczne, weryfikowalne warunki, np. w formacie Given/When/Then, z trwałymi identyfikatorami.
- **Przypadki brzegowe i ograniczenia:** Uwzględnij sytuacje nietypowe oraz istotne wymagania niefunkcjonalne.

Przykład ustrukturyzowanego zapisu:

```text
US-01: Jako użytkownik chcę przesłać plik, aby przekazać dane do analizy.

AC-01:
Given: użytkownik wybrał plik w obsługiwanym formacie
When: użytkownik rozpoczyna przesyłanie
Then: system przyjmuje plik i potwierdza jego odebranie
```

### Specyfikacja poprawki: bugfix.md

- **Opis i odtworzenie błędu:** Przedstaw problem, warunki jego wystąpienia oraz kroki reprodukcji.
- **Obecne zachowanie:** Opisz, co system robi niepoprawnie i w jakich okolicznościach.
- **Oczekiwane zachowanie:** Określ, jak system powinien działać po naprawie, wraz z weryfikowalnymi kryteriami.
- **Zachowanie bez zmian:** Wskaż istniejące zachowania, których poprawka nie może naruszyć.
- **Analiza przyczyny i zakres:** Oddziel potwierdzone ustalenia od hipotez oraz określ granice naprawy.

Przykład ustrukturyzowanego zapisu:

```text
BUG-01: Nieprawidłowa obsługa pustego pliku.

CURRENT-01: Przesłanie pustego pliku powoduje nieobsłużony wyjątek.
EXPECTED-01: System odrzuca pusty plik i wyświetla komunikat walidacyjny.
UNCHANGED-01: Przesyłanie poprawnych, niepustych plików działa jak dotychczas.
```

## Projekt techniczny: design.md

Druga faza określa, jak zrealizować wymagania lub naprawić błąd. Projekt techniczny powinien odnosić decyzje implementacyjne do ustaleń z pierwszej fazy.

- **Architektura systemu:** Opisz elementy objęte zmianą, ich odpowiedzialności, zależności i punkty integracji.
- **Projekt komponentów:** Zdefiniuj interfejsy, kontrakty API, modele danych i podział odpowiedzialności.
- **Diagramy sekwencji:** Przedstaw kolejność interakcji dla najważniejszych scenariuszy, np. za pomocą diagramów Mermaid.
- **Przepływ danych:** Określ źródła danych, walidację, przekształcenia, przechowywanie i wyniki.
- **Obsługa błędów:** Opisz wykrywanie błędów, komunikaty, propagację wyjątków i ewentualne mechanizmy odzyskiwania.
- **Strategia testowania:** Zaplanuj właściwe testy jednostkowe, integracyjne lub end-to-end; dla poprawek uwzględnij reprodukcję błędu i testy regresji.
- **Uwarunkowania implementacji:** Zapisz ograniczenia, ryzyka, kompromisy oraz wymagania dotyczące kompatybilności, migracji, bezpieczeństwa i wydajności, jeśli mają zastosowanie.

## Zadania: tasks.md

Trzecia faza przekształca projekt techniczny w szczegółowy plan implementacji. Każde zadanie powinno być odrębne, możliwe do wykonania i zakończone sprawdzalnym rezultatem.

- **Identyfikator i zakres:** Nadaj zadaniu trwały identyfikator i opisz konkretną zmianę.
- **Powiązanie ze specyfikacją:** Wskaż realizowane wymagania, kryteria akceptacji lub zachowania z analizy błędu.
- **Zależności:** Określ zadania, które muszą zostać wykonane wcześniej.
- **Oczekiwany rezultat:** Zdefiniuj warunek ukończenia oraz sposób jego weryfikacji.
- **Status:** Aktualizuj postęp na bieżąco podczas implementacji, a nie dopiero po zakończeniu wszystkich prac.

Przykładowy zapis:

```markdown
- [ ] T-01: Zaimplementować walidację pustego pliku.
  - Powiązania: EXPECTED-01, UNCHANGED-01.
  - Zależności: brak.
  - Rezultat: pusty plik jest odrzucany z komunikatem; poprawne pliki nadal są przyjmowane.
  - Weryfikacja: test walidacji i test regresji dla poprawnego pliku.
  - Status: do zrobienia.
```

Zalecane statusy to `do zrobienia`, `w toku`, `zablokowane` i `ukończone`. Pole wyboru `[x]` oznacza zadanie ukończone i zweryfikowane; przy blokadzie należy dopisać jej przyczynę oraz warunek odblokowania.

### Sposoby realizacji

- **Pojedyncze zadanie:** Wybierz konkretne zadanie, sprawdź jego zależności, wykonaj zmianę, zweryfikuj rezultat i zaktualizuj status.
- **Wszystkie zadania:** Realizuj cały plan z uwzględnieniem zależności, aktualizując status każdego zadania na bieżąco. Ten tryb nie oznacza wykonywania wszystkich zadań równolegle.

## Przepływ pracy i warunki ukończenia

Praca przechodzi od `requirements.md` lub `bugfix.md` przez `design.md` do `tasks.md`, a następnie do implementacji i weryfikacji. Jeśli w trakcie implementacji zmieniają się założenia, zaktualizuj odpowiednie dokumenty, aby specyfikacja i kod pozostały spójne.

- **Wymagania lub analiza błędu:** Zakres i oczekiwane zachowania są jednoznaczne oraz możliwe do sprawdzenia.
- **Projekt techniczny:** Podejście implementacyjne, przepływy, obsługa błędów i strategia testowania są opisane.
- **Plan zadań:** Zadania mają jasne rezultaty, zależności i sposób weryfikacji.
- **Implementacja:** Wszystkie wymagane zadania są ukończone, kryteria spełnione, a wyniki testów odnotowane. Dla poprawki potwierdzono również zachowanie funkcji, które miały pozostać bez zmian.
