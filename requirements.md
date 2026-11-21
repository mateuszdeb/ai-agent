# Wymagania: lokalny interfejs przeglądarkowy agenta wydatków

Status: propozycja wymagań do akceptacji. Dokument stanowi pierwszą fazę procesu z `workflow.md`; nie określa jeszcze architektury ani listy zadań implementacyjnych.

## Problem i kontekst

Household Expense Checker Agent ma działającą pętlę agenta, narzędzia domenowe, kontroler wykonania, ledger zmian i serwer MCP. Dotychczasowe uruchamianie przez terminal nie zapewnia jednak przeglądarkowego formularza poleceń, widoku wyników ani interaktywnego potwierdzania operacji wrażliwych.

Celem rozszerzenia jest umożliwienie korzystania z istniejącego agenta przez prosty interfejs w przeglądarce. Logika domenowa i decyzja o dopuszczeniu wykonania pozostają po stronie aplikacji Python; odpowiedź modelu ani kod przeglądarki nie zastępują tej kontroli.

## Cele

- **G-01: Dostępność lokalna:** Użytkownik otwiera aplikację pod lokalnym adresem podanym po uruchomieniu, bez uruchamiania osobnego polecenia terminalowego dla każdej analizy.
- **G-02: Pełny przebieg w UI:** Użytkownik wprowadza cel, uruchamia analizę oraz widzi jej status i wynik w jednej aplikacji.
- **G-03: Bezpieczne decyzje:** Brak jawnej, zweryfikowanej decyzji użytkownika oznacza brak wykonania akcji wrażliwej.
- **G-04: Przejrzystość:** Użytkownik odróżnia propozycję modelu, wykonane działanie, odmowę oraz błąd i może powiązać wynik z identyfikatorem przebiegu.
- **G-05: Zachowanie implementacji:** Interfejs wykorzystuje istniejące narzędzia, walidację i stan, bez tworzenia odrębnej logiki budżetowej.

## Zakres i założenia

Poniższe założenia wyznaczają proponowany MVP, a nie dodatkowe decyzje już podjęte przez użytkownika.

- **Użytkownik:** Jedna osoba pracująca lokalnie na komputerze z projektem i skonfigurowanym modelem.
- **Dostęp:** Przeglądarka na tym samym komputerze, przez loopback. Dostęp z telefonu, sieci LAN lub Internetu nie jest częścią MVP.
- **Język:** Interfejs w języku angielskim: etykiety, komunikaty, instrukcje, walidacja, statusy i pytania potwierdzające. Nazwy narzędzi, statusy techniczne i identyfikatory pozostają zgodne z backendem. Treść danych i polecenia użytkownika nie są automatycznie tłumaczone.
- **Dane:** Obecny, zanonimizowany zbiór projektu oraz jego efektywny stan wynikający z ledgera.
- **Model:** Konfiguracja po stronie aplikacji, poza formularzami przeglądarki. UI nie wymaga wpisywania kluczy API.
- **Przebieg:** Samodzielne polecenie i wynik, z możliwością podjęcia konkretnej oczekującej decyzji. Pełna, wielowątkowa historia rozmów nie jest wymagana.
- **Równoległość:** Jeden aktywny przebieg na instancję aplikacji; zapisy są kontrolowane także między kartami tej samej przeglądarki.

## Poza zakresem

- **Hosting i konta:** Publiczne wdrożenie, logowanie, wielu użytkowników i synchronizacja między urządzeniami.
- **Integracje finansowe:** Połączenia z bankami, przelewy, płatności i dane uwierzytelniające bankowości.
- **Import i multimedia:** Import nowych wyciągów, przesyłanie zdjęć, OCR i analiza paragonów.
- **Rozbudowany panel:** Wykresy budżetowe, prognozy, raporty okresowe i eksporty danych.
- **Zarządzanie modelem:** Pobieranie modeli, instalowanie Ollama i edycja sekretów z UI.
- **Osobna implementacja domeny:** Kopiowanie działania narzędzi do frontendu, zmiana kontraktu MCP lub zastępowanie istniejącego ledgera nową bazą.

## Historyjki użytkownika i wymagania

Priorytet P0 oznacza zakres konieczny do odbioru MVP. P1 oznacza rozszerzenia niewstrzymujące odbioru podstawowego interfejsu; nie należy automatycznie włączać ich do pierwszej implementacji.

### US-01: Otwarcie aplikacji lokalnej

Jako użytkownik chcę otworzyć interfejs w przeglądarce, aby korzystać z agenta bez wykonywania poleceń terminalowych dla każdej operacji.

Priorytet: P0. Powiązania: G-01.

```text
AC-01:
Given: zależności są zainstalowane i użytkownik znajduje się w katalogu projektu
When: uruchamia aplikację poleceniem udokumentowanym w README
Then: otrzymuje lokalny adres i po otwarciu go widzi formularz polecenia oraz stan aplikacji

AC-02:
Given: aplikacja działa w konfiguracji domyślnej
When: uruchamia usługę przeglądarkową
Then: usługa nasłuchuje wyłącznie na interfejsie loopback i nie wymaga zewnętrznego hostingu

AC-03:
Given: lokalny model jest niedostępny
When: użytkownik otwiera aplikację
Then: UI nadal się ładuje, a próba analizy zwraca zrozumiały błąd połączenia, bez pozornego sukcesu
```

### US-02: Wprowadzenie celu analizy

Jako użytkownik chcę wpisać polecenie w języku naturalnym, aby zlecić agentowi sprawdzenie wydatków.

Priorytet: P0. Powiązania: G-02. Zależność: US-01.

```text
AC-04:
Given: nie trwa inny przebieg
When: użytkownik wpisuje niepuste polecenie i wybiera uruchomienie
Then: aplikacja rozpoczyna dokładnie jeden przebieg i nadaje mu identyfikator

AC-05:
Given: polecenie jest puste albo składa się wyłącznie z białych znaków
When: użytkownik próbuje rozpocząć analizę
Then: UI pokazuje błąd walidacji, a backend nie wywołuje modelu ani narzędzi

AC-06:
Given: użytkownik dwukrotnie naciska uruchomienie albo ponawia to samo żądanie po opóźnieniu sieci
When: żądanie dotyczy tej samej operacji rozpoczęcia przebiegu
Then: backend nie tworzy drugiego przebiegu i nie powiela jego skutków
```

### US-03: Obserwowanie postępu i wyniku

Jako użytkownik chcę widzieć, czy agent pracuje i co ustalił, aby odróżniać oczekiwanie od zakończenia lub awarii.

Priorytet: P0. Powiązania: G-02, G-04. Zależność: US-02.

```text
AC-07:
Given: backend przyjął polecenie
When: analiza jeszcze trwa
Then: UI pokazuje stan pracy oraz identyfikator przebiegu i blokuje rozpoczęcie kolejnego przebiegu

AC-08:
Given: agent zakończył analizę
When: UI otrzymuje wynik
Then: pokazuje odpowiedź końcową, status zakończenia i identyfikator przebiegu, zachowując czytelność list, kwot i identyfikatorów transakcji

AC-09:
Given: model osiągnął limit kroków, timeout albo zwrócił błąd
When: przebieg się kończy
Then: UI odróżnia ten stan od sukcesu, zachowuje dostępne wyniki i nie uruchamia automatycznego ponowienia operacji

AC-10:
Given: użytkownik odświeża stronę albo traci połączenie z UI podczas analizy
When: strona zostaje ponownie otwarta
Then: aplikacja odczytuje dostępny stan lub wyświetla, że wymaga on ponownego sprawdzenia; nie wysyła ponownie polecenia i nie uznaje nieznanego stanu za sukces
```

### US-04: Przeglądanie transakcji

Jako użytkownik chcę zobaczyć transakcje używane przez agenta, aby rozumieć jego wyniki i móc sprawdzić wskazany rekord.

Priorytet: P0. Powiązania: G-04, G-05.

```text
AC-11:
Given: dane są dostępne
When: użytkownik otwiera widok transakcji
Then: widzi identyfikator, datę, sprzedawcę, kwotę, walutę i kategorię z efektywnego stanu danych, a nie wyłącznie z bazowego JSON

AC-12:
Given: użytkownik podaje frazę lub sprzedawcę
When: filtruje listę
Then: wyniki są zgodne z istniejącą semantyką wyszukiwania; odczyt nie wywołuje modelu i nie zapisuje zmian

AC-13:
Given: lista przekracza limit pojedynczego odczytu albo nie znaleziono rekordów
When: UI wyświetla wyniki
Then: jasno wskazuje ograniczenie wyników albo pustą listę, bez sugerowania kompletności uciętego zbioru i bez oznaczania pustego wyniku jako awarii

AC-14:
Given: użytkownik wybiera identyfikator istniejącej transakcji
When: otwiera szczegóły
Then: otrzymuje dane tej transakcji; dla nieistniejącego identyfikatora widzi komunikat zamiast pustego lub błędnego rekordu
```

### US-05: Świadome potwierdzenie operacji wrażliwej

Jako użytkownik chcę jawnie zatwierdzić lub odrzucić usunięcie transakcji, aby model nie mógł wykonać go samodzielnie.

Priorytet: P0. Powiązania: G-03, G-04, G-05. Zależności: US-02, US-03.

```text
AC-15:
Given: kontroler zgłosił confirmation_required
When: UI przedstawia oczekującą decyzję
Then: pokazuje nazwę operacji, action_id, identyfikator i podstawowe dane transakcji, powód oraz informację, że operacja nie została jeszcze wykonana

AC-16:
Given: istnieje ważna, zapisana po stronie aplikacji oczekująca akcja
When: użytkownik wybiera jawne potwierdzenie
Then: backend weryfikuje powiązanie decyzji z tą akcją, przebiegiem, parametrami i aktualnym stanem, a następnie dopuszcza wyłącznie zatwierdzoną operację przez kontroler

AC-17:
Given: użytkownik odmawia, nie podejmuje decyzji, zamyka stronę albo backend traci kontekst oczekującej akcji
When: nie istnieje ważne potwierdzenie
Then: akcja nie jest wykonywana; odmowa jest widoczna jako wynik, a utracony kontekst wymaga nowej, zweryfikowanej propozycji

AC-18:
Given: żądanie pochodzi z tekstu modelu, modelowych argumentów confirmed=True, dowolnej treści czatu albo zawiera podmienione parametry lub nieznany identyfikator
When: próbuje autoryzować akcję
Then: backend odrzuca je bez modyfikowania danych; sama znajomość action_id i sam tekst zgody nie stanowią autoryzacji

AC-19:
Given: potwierdzenie zostało już zużyte albo stan docelowej transakcji się zmienił
When: przychodzi powtórzone lub nieaktualne żądanie
Then: aplikacja nie wykonuje akcji ponownie ani wobec innego celu; zwraca bieżący wynik lub wymaga nowej decyzji
```

Potwierdzenie dotyczy pojedynczej akcji, nigdy całego przebiegu ani wszystkich przyszłych wywołań. UI nie może używać hooka, który automatycznie zatwierdza każde żądanie, ani przesyłać do modelu sekretu służącego do zatwierdzania.

### US-06: Rozróżnianie propozycji i wykonanych zmian

Jako użytkownik chcę wiedzieć, które działania rzeczywiście wykonała aplikacja, aby nie opierać się wyłącznie na deklaracji modelu.

Priorytet: P0. Powiązania: G-03, G-04, G-05. Zależności: US-03, US-05.

```text
AC-20:
Given: narzędzie zwróciło wynik
When: UI prezentuje informację o działaniu
Then: stan wykonania wynika z wyniku kontrolera, nie z samej odpowiedzi modelu; narzędzie, status i dostępny action_id są widoczne w podsumowaniu

AC-21:
Given: narzędzie zwróciło already_assigned, not_found, state_conflict, invalid_arguments albo unknown_tool
When: wynik jest prezentowany
Then: użytkownik widzi zrozumiały komunikat bez deklaracji wykonania zmiany; istniejąca kategoria i konflikt stanu nie są ukrywane

AC-22:
Given: model proponuje nadpisanie istniejącej kategorii
When: brak jawnej decyzji użytkownika dotyczącej tego nadpisania
Then: ścieżka przeglądarkowa nie wykonuje wymuszonego zapisu tylko na podstawie modelowego force_override=True

AC-23:
Given: operacja została wykonana albo odrzucona
When: użytkownik wraca do listy transakcji
Then: UI odświeża efektywny stan i nie pozostawia zmienionych wizualnie danych po operacji, która nie doszła do skutku
```

### US-07: Obsługa problemów bez utraty kontroli

Jako użytkownik chcę otrzymywać czytelne komunikaty o błędach i zajętości aplikacji, aby wiedzieć, czy mogę bezpiecznie ponowić działanie.

Priorytet: P0. Powiązania: G-02, G-03.

```text
AC-24:
Given: model, dane albo zapis ledgera są niedostępne
When: operacja napotyka błąd
Then: UI podaje kategorię problemu i możliwy następny krok, bez ujawniania sekretów, surowego stosu wyjątków lub prywatnych ścieżek

AC-25:
Given: trwa przebieg, oczekuje decyzja albo wykonywany jest zapis
When: druga karta lub drugie żądanie próbuje rozpocząć kolidującą operację
Then: backend odrzuca ją lub bezpiecznie serializuje; sama blokada przycisku w pierwszej karcie nie jest jedynym zabezpieczeniem

AC-26:
Given: po wysłaniu zapisu UI nie zna wyniku z powodu utraty połączenia
When: użytkownik wraca do aplikacji
Then: może sprawdzić status po identyfikatorze; UI nie wykonuje w ciemno ponownego zapisu
```

### US-08: Podgląd szczegółów przebiegu

Jako użytkownik chcę rozwinąć szczegóły techniczne analizy, aby zrozumieć decyzje agenta bez czytania pliku JSONL w terminalu.

Priorytet: P1. Powiązania: G-04. Zależność: US-03.

```text
AC-27:
Given: istnieje ślad przebiegu
When: użytkownik otwiera jego szczegóły
Then: widzi kolejność narzędzi, ich statusy, model oraz dostępne dane o opóźnieniu i tokenach; brakujące pomiary są oznaczone jako niedostępne, a nie zastępowane wymyślonymi wartościami
```

### US-09: Ręczna korekta i cofnięcie kategorii

Jako użytkownik chcę ręcznie skorygować kategorię i cofnąć własny zapis, aby nie używać do każdej korekty modelu.

Priorytet: P1. Powiązania: G-03, G-05. Zależności: US-04, US-06.

```text
AC-28:
Given: użytkownik wybiera transakcję i kategorię
When: zapisuje zmianę, a przy nadpisaniu jawnie zatwierdza pokazane stare i nowe wartości
Then: aplikacja korzysta z istniejącego assign_category, prezentuje wynik i action_id oraz nie wywołuje modelu

AC-29:
Given: użytkownik wybiera cofnięcie przypisania
When: backend weryfikuje action_id
Then: korzysta z undo_category_assignment i pokazuje wynik; akcje nieistniejące, już cofnięte i kolidujące z nowszym zapisem nie powodują zmiany danych
```

## Wymagania niefunkcjonalne

- **NFR-01: Ponowne użycie logiki (P0):** Wszystkie operacje domenowe z UI przechodzą przez istniejący kontroler i narzędzia albo wspólną warstwę aplikacyjną zachowującą ich walidację. Zabezpieczenia HTTP i stan oczekujących decyzji mogą być nową warstwą; nie wolno przy tym obchodzić kontroli backendu.
- **NFR-02: Niezmienność danych (P0):** Odczyty nie tworzą zdarzeń, a zapisy używają istniejącego ledgera. Hash bazowego `data/transactions.json` pozostaje niezmieniony w testach odczytu, przypisania, cofnięcia, zatwierdzenia i odmowy.
- **NFR-03: Lokalność i sekrety (P0):** UI nie wprowadza zewnętrznej analityki ani usług chmurowych. Konfiguracja i ewentualne klucze pozostają po stronie serwera; nie są zwracane do przeglądarki ani zapisywane w jej pamięci. Tryb MVP korzysta z lokalnego modelu bez automatycznego przełączania na chmurę.
- **NFR-04: Bezpieczeństwo treści (P0):** Dane transakcji i tekst modelu są niezaufane. HTML lub skrypty umieszczone w tych treściach nie wykonują się; prezentacja Markdown, jeśli zastosowana, nie umożliwia aktywnej treści.
- **NFR-05: Ochrona operacji (P0):** Obca witryna nie może rozpocząć analizy, zmienić danych ani zatwierdzić akcji przez przeglądarkę użytkownika. Backend weryfikuje pochodzenie i uprawnienie żądań zmieniających stan; samo ograniczenie nasłuchu do localhost nie wystarcza.
- **NFR-06: Obsługa UI (P0):** Podstawowy przebieg jest dostępny klawiaturą, pola i przyciski mają etykiety, a stan lub błąd nie jest sygnalizowany wyłącznie kolorem. Przy szerokości widoku 360 px polecenie, wynik i decyzja potwierdzenia pozostają dostępne; tabela może mieć własne przewijanie poziome.
- **NFR-07: Responsywność (P0):** W teście lokalnym ze stubem modelu oczekującym na wynik UI pokazuje przyjęcie polecenia lub stan oczekiwania w ciągu 1 sekundy od odpowiedzi backendu i pozostaje interaktywne. Nie ustala się gwarancji czasu generowania odpowiedzi żywego modelu; obowiązują jawne limity i obsługa timeoutów istniejącej aplikacji.
- **NFR-08: Zgodność (P0):** Istniejące CLI, MCP i demonstracja wielonarzędziowa pozostają działające; dodanie UI nie nadpisuje dowodów w `evidence/`. Dotychczasowe testy nadal przechodzą.
- **NFR-09: Uruchomienie (P0):** README podaje wymagane polecenia, adres lokalny, konfigurację modelu i sposób zatrzymania aplikacji. Otwarcie strony nie uruchamia analizy ani nie wymaga połączenia z modelem.
- **NFR-10: Angielski interfejs (P0):** Wszystkie stałe teksty UI, w tym błędy i decyzje potwierdzenia, są po angielsku. Wyniki narzędzi są mapowane na angielskie komunikaty prezentacyjne, bez zmiany danych i kodów statusów. Odpowiedzi modelu są domyślnie proszone po angielsku; treści użytkownika, nazwy sprzedawców oraz odpowiedzi w innym języku wyraźnie zamówione przez użytkownika nie podlegają temu ograniczeniu.

## Mierniki i odbiór

Nie ma jeszcze pomiarów użyteczności nowego interfejsu. Poniższe wartości są kryteriami odbioru proponowanego MVP, a nie deklaracją osiągniętych wyników.

- **M-01: Pokrycie zakresu:** Wszystkie AC-01–AC-26 oraz NFR oznaczone P0 są spełnione. AC-27–AC-29 pozostają poza obowiązkowym odbiorem, dopóki zakres nie zostanie rozszerzony.
- **M-02: Ochrona danych:** W testach odmowy, braku zgody, podmienionej zgody i powtórzonego zatwierdzenia nie występuje nieautoryzowany zapis; bazowy plik danych zachowuje hash.
- **M-03: Zgodność działania:** Testy integracyjne wykazują tę samą semantykę odczytu, zapisu i statusów co istniejące narzędzia; nowe testy UI nie wymagają żywego modelu.
- **M-04: Demonstracja użytkowa:** Osobno wykonany przebieg z lokalnym modelem pokazuje co najmniej dwa wywołania narzędzi oraz reakcję na obserwację. Użytkownik może odmówić akcji w UI bez zmiany stanu; osobny, kontrolowany scenariusz potwierdzenia na danych testowych wykonuje dokładnie jedną zatwierdzoną akcję.
- **M-05: Regresja:** Dotychczasowy zestaw testów, nowe testy kryteriów MVP i Ruff kończą się bez błędów. Dowody z UI są odrębne od śladu demonstracji wielonarzędziowej.

Odbiór obejmuje: odczyt i pusty wynik, poprawną analizę, niedostępność modelu, limit kroków, odświeżenie podczas pracy, odrzucenie i zaakceptowanie akcji, duplikat żądania, nieaktualny stan, próbę podmiany celu oraz niezaufaną treść w odpowiedzi.

## Otwarte decyzje

- **OQ-01: Stos UI (nieblokująca dla wymagań):** Framework, sposób komunikacji i aktualizacji postępu wybiera etap `design.md`. Preferencja produktu: najmniejsza złożoność wystarczająca do realizacji P0. Odpowiedzialny: autor projektu technicznego.
- **OQ-02: Zakres P1 (nieblokująca dla MVP):** Przed planowaniem rozszerzeń użytkownik decyduje, czy szczegóły przebiegu oraz ręczna korekta i cofanie mają wejść do pierwszej wersji. Domyślnie nie są wymagane.
- **OQ-03: Dostęp spoza komputera (poza MVP):** Wymaga osobnej decyzji użytkownika i rozszerzenia wymagań bezpieczeństwa; nie należy włączać go przez samo domyślne nasłuchiwanie na wszystkich interfejsach.

Kolejne dokumenty procesu to `design.md` i `tasks.md`. Plan narzędzi i integracji MCP jest w `PLAN.md`.
