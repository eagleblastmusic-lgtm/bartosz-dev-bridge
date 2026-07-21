# BDB Project Creator — Control Center 0.3.1 / extension 0.2.9

## Cel

Przycisk **Kreator projektu** w Control Center uruchamia jeden potwierdzony przebieg dla nowego albo istniejącego projektu:

```text
wybór trybu → repozytorium Git → GitHub → Prepare → Start → Re-arm → prompt w ChatGPT
```

Po uruchomieniu prompt prowadzi rozmowę do normalnej pętli BDB: kontekst, analiza, dozwolone edycje, test, poprawka, promocja i końcowe potwierdzenie czystego źródła.

## Nowy projekt

Kreator:

1. tworzy pusty katalog projektu;
2. zapisuje `README.md` i `.gitignore`;
3. inicjalizuje branch `main` i lokalny commit;
4. sprawdza lokalne `gh auth status`;
5. tworzy prywatne albo publiczne repo przez `gh repo create`;
6. dodaje `origin` i wysyła wyłącznie commit inicjalizacyjny;
7. przygotowuje workspace BDB przez istniejący `ProjectPrepareService`;
8. uruchamia Bridge i uzbraja Native Host;
9. kolejkuje prompt i otwiera ChatGPT.

GitHub CLI działa bez powłoki (`shell=False`). Kreator nie wykonuje merge, deployu ani późniejszych pushy kodu wygenerowanego przez BDB.

## Istniejący projekt

Źródłem może być:

- czysty lokalny checkout Git przypięty do brancha;
- URL `https://github.com/owner/repo[.git]`;
- adres SSH `git@github.com:owner/repo[.git]`.

Dla URL repozytorium jest klonowane do wskazanego katalogu projektów. Dla lokalnego checkoutu Kreator nie kopiuje ani nie przebudowuje repo.

## Przekazanie prompta

Control Center zapisuje jeden ograniczony prompt do lokalnej kolejki obok konfiguracji Native Host. Rozszerzenie 0.2.9:

1. odczytuje oczekujący prompt przez Native Messaging;
2. zdobywa 45-sekundową dzierżawę przypisaną do jednej karty ChatGPT;
3. nie dotyka zajętego edytora;
4. wstawia prompt tylko do pustego edytora;
5. opcjonalnie wysyła go z tym samym potwierdzonym mechanizmem co AUTO;
6. usuwa kolejkę dopiero po potwierdzonej obsłudze.

Dzierżawa zapobiega jednoczesnemu wstawieniu tego samego prompta do kilku kart. Po awarii wygasa i pozwala innej karcie bezpiecznie wznowić przekazanie.

## Bezpieczeństwo

- wymagane jest jedno jawne potwierdzenie całego planu w oknie Kreatora;
- alias, nazwa repo, URL, prompt, limity i allowlista są walidowane przed mutacją;
- istniejący workspace lub katalog nowego projektu nie jest nadpisywany;
- brudny albo detached checkout jest odrzucany przez istniejący preflight;
- polecenia są ograniczone do katalogu `git`/`gh`; nie ma dowolnej powłoki;
- przy błędzie artefakty pozostają do inspekcji; nie ma automatycznego cleanupu;
- start prompt nie udziela zgody na push, merge ani deploy kodu zadania.

## Wymagania operatorskie

- Git w `PATH`;
- dla nowego repo: GitHub CLI `gh` w `PATH` i aktywne logowanie;
- aktualny Control Center 0.3.1;
- rozszerzenie 0.2.9;
- windowless Native Host z bieżącego źródła;
- pusty edytor w co najmniej jednej karcie ChatGPT.
