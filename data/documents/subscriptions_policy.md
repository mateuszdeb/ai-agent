# Zasady Zarządzania Subskrypcjami i Płatnościami Cyklicznymi (Subscriptions Policy)

**Dokument źródłowy:** `data/documents/subscriptions_policy.md`  
**Wersja:** 2026.1  
**Status:** Obowiązujący w gospodarstwie domowym  

---

## 1. Dozwolone usługi cyfrowe i plany taryfowe

W ramach kategorii `Subscriptions` dopuszczone są wyłącznie usługi o udokumentowanej wartości dla domowników:
1. **Audio streaming (Spotify Polska):** Dopuszczalny plan indywidualny o stawce **24,99 PLN miesięcznie** lub plan rodzinny do 43,99 PLN miesięcznie.
2. **Video streaming (VOD):** Maksymalnie dwa aktywne serwisy VOD w tym samym miesiącu kalendarzowym.
3. **Usługi chmurowe i deweloperskie (np. PayPal XYZ_CLOUD_SRV):** Wszelkie subskrypcje narzędzi programistycznych, serwerów lub hostingu opłacane przez bramki płatnicze (np. PayPal) wymagają uzasadnienia i pobrania faktury VAT w terminie do 14 dni od pobrania opłaty.

---

## 2. Detekcja zduplikowanych obciążeń (Duplicate Charges Policy)

Wystąpienie dwóch lub więcej obciążeń od tego samego dostawcy (np. Spotify Polska) w odstępie krótszym niż **48 godzin** na identyczną kwotę (np. 24,99 PLN) jest z zasady traktowane jako błąd rozliczeniowy operatora płatności lub przypadkowe podwójne zamówienie.

### Procedura postępowania z duplikatem:
1. Natychmiastowe oflagowanie drugiej transakcji jako `duplicate`.
2. Zgłoszenie reklamacji do usługodawcy lub wystawienie wniosku o chargeback w banku.
3. W systemie zarządczym transakcja oznaczona jako błąd powinna zostać skierowana do procedury usunięcia / korekty z wymaganym potwierdzeniem autoryzacyjnym użytkownika.

---

## 3. Anulowanie i rezygnacja z subskrypcji

1. **Okres wypowiedzenia:** Wypowiedzenie usługi płatnej z góry musi nastąpić co najmniej na **48 godzin przed końcem bieżącego okresu rozliczeniowego**, aby uniknąć naliczenia opłaty za kolejny miesiąc.
2. **Audyt kwartalny:** W ostatnim tygodniu każdego kwartału przeprowadzany jest przegląd historii transakcji pod kątem subskrypcji nieużywanych przez okres dłuższy niż 30 dni.
