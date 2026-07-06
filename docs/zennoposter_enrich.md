# ZennoPoster 7.9 — дообогащение checko по ИНН (1 аккаунт)

Проект берёт список ИНН (уже спарсенный), под **одним** залогиненным аккаунтом
ходит по checko, находит компанию по ИНН, открывает карточку и вытягивает
телефоны/почты/сайты. Пишет в CSV, **докачиваемо** (уже обработанные ИНН
пропускает), с вежливыми паузами. Без ротации аккаунтов/прокси и без
авторешения капчи: при блоке — пауза, капчу решаешь вручную в открытом браузере.

## Файлы

- `inns.txt` — список ИНН (по одному в строке; можно CSV — берётся первая колонка).
- `full_site.csv` — результат (создаётся сам; сюда же докачивает).

## Переменные проекта (Project → Variables)

| Имя | Пример | Смысл |
|-----|--------|-------|
| `inputPath`  | `C:\zp\inns.txt`        | файл со списком ИНН |
| `outputPath` | `C:\zp\full_site.csv`   | куда писать результат |
| `login`      | `you@mail.ru`           | логин аккаунта checko |
| `password`   | `ваш_пароль`            | пароль |
| `delayMin`   | `3`                     | мин. пауза между ИНН, сек |
| `delayMax`   | `7`                     | макс. пауза, сек |
| `proxy`      | (пусто или `host:port:user:pass`) | ОДИН статичный прокси (по желанию) |

## Схема проекта (кубы)

1. **Старт браузера** (обычный старт ZennoPoster).
2. **(Опц.) Прокси** — куб «C# code»:
   ```csharp
   string px = project.Variables["proxy"].Value.Trim();
   if (px.Length > 0) instance.SetProxy(px, true, true, true, true);
   ```
3. **Логин** — один раз за запуск. Проще всего записать рекордером ZennoPoster:
   открыть `https://checko.ru/`, кнопка «Войти», заполнить поля логином/паролем
   (из переменных `{-Variable.login-}` / `{-Variable.password-}`), нажать «Войти».
   Либо, если уже вошёл в профиле браузера — этот шаг пропусти.
4. **Главный цикл** — один куб «C# code» с кодом ниже (делает всё: читает ИНН,
   докачку, обход, парсинг, запись, паузы). Это самый надёжный вариант — весь
   цикл внутри одного C#-куба.

## Код главного куба (C# code / Общий код)

```csharp
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Collections.Generic;

string inputPath  = project.Variables["inputPath"].Value;
string outputPath = project.Variables["outputPath"].Value;
int delayMin = int.Parse(project.Variables["delayMin"].Value);
int delayMax = int.Parse(project.Variables["delayMax"].Value);
var rnd = new Random();

// экранирование поля CSV (объявляем ДО использования — ZP не любит local functions)
Func<string,string> Q = s => {
    if (s == null) return "";
    if (s.Contains(";") || s.Contains("\"") || s.Contains("\n") || s.Contains("\r"))
        return "\"" + s.Replace("\"", "\"\"") + "\"";
    return s;
};

// читаем ИНН (первая колонка строки)
var inns = new List<string>();
foreach (var line in File.ReadAllLines(inputPath, Encoding.UTF8)) {
    var s = line.Split(';')[0].Trim();
    if (Regex.IsMatch(s, @"^\d{10}$|^\d{12}$")) inns.Add(s);
}

// уже обработанные (докачка)
var done = new HashSet<string>();
if (File.Exists(outputPath)) {
    foreach (var line in File.ReadAllLines(outputPath, Encoding.UTF8)) {
        var s = line.Split(';')[0].Trim();
        if (s.Length > 0) done.Add(s);
    }
} else {
    File.WriteAllText(outputPath,
        "ИНН;ОГРН;Название;Регион;Телефоны;Почты;Сайты;Ошибка\r\n", Encoding.UTF8);
}

string[] badSites = { "googleapis","gstatic","yastatic","mc.yandex","an.yandex",
  "yandex.ru/maps","yandex.ru/clck","chrome.google","play.google","apps.apple",
  "webstore","2gis","yell.ru","zoon","google-analytics","doubleclick",
  ".png",".jpg",".jpeg",".svg",".css",".js",".ico",".woff","checko.ru","nalog.",
  "zakupki","gov.ru","rusprofile","list-org","sbis.ru","audit-it","datanewton",
  "fedresurs","kad.arbitr","consultant.ru" };
string[] badMail = { "trashlify","example.com","sentry","noreply","no-reply",".png",".jpg" };

int okCount = 0, errCount = 0;

foreach (var inn in inns) {
    if (done.Contains(inn)) continue;

    string ogrn = "", name = "", region = "", err = "";
    var phones = new List<string>(); var emails = new List<string>(); var sites = new List<string>();
    try {
        // 1) поиск по ИНН
        instance.ActiveTab.Navigate("https://checko.ru/search?query=" + inn, "");
        instance.ActiveTab.WaitDownloading();
        System.Threading.Thread.Sleep(2500);            // дать JS SPA отрисоваться
        string html = instance.ActiveTab.DocumentText;
        string curUrl = instance.ActiveTab.URL;

        // блок / капча -> пауза (решаешь вручную в открытом браузере)
        if (html.Contains("captcha") || html.Contains("Доступ ограничен")
            || html.Contains("Слишком много запросов") || html.Contains("429")) {
            project.SendInfoToLog("Блок/капча на ИНН " + inn + " — пауза 60 c, реши капчу вручную", true);
            System.Threading.Thread.Sleep(60000);
            html = instance.ActiveTab.DocumentText;
            curUrl = instance.ActiveTab.URL;
        }

        // ОГРН из URL (редирект на карточку) или из первой ссылки на компанию
        var m = Regex.Match(curUrl, @"-(\d{13})(?:[/?#]|$)");
        if (!m.Success) m = Regex.Match(html, @"/company/[^""']*?-(\d{13})");
        if (!m.Success) throw new Exception("ОГРН не найден по ИНН");
        ogrn = m.Groups[1].Value;

        // 2) карточка по ОГРН (если ещё не на ней)
        if (!curUrl.Contains(ogrn)) {
            instance.ActiveTab.Navigate("https://checko.ru/company/" + ogrn, "");
            instance.ActiveTab.WaitDownloading();
            System.Threading.Thread.Sleep(2500);
            html = instance.ActiveTab.DocumentText;
        }

        // название / регион из <title>
        var t = Regex.Match(html, @"<title>(.*?)</title>", RegexOptions.Singleline);
        if (t.Success) {
            var segs = Regex.Split(t.Groups[1].Value, @"\s[–—-]\s");
            if (segs.Length > 0) name = segs[0].Trim();
            if (segs.Length > 1 && !segs[1].Trim().StartsWith("ИНН")) region = segs[1].Trim();
        }

        // контакты
        foreach (Match x in Regex.Matches(html, @"tel:([+\d\s\(\)\-]{6,})"))
            phones.Add(x.Groups[1].Value.Trim());
        foreach (Match x in Regex.Matches(html, @"mailto:([^""'?<>]+)")) {
            var e = x.Groups[1].Value.Trim();
            bool bad = false; foreach (var b in badMail) if (e.ToLower().Contains(b)) bad = true;
            if (!bad && e.Contains("@")) emails.Add(e);
        }
        foreach (Match x in Regex.Matches(html, @"href=""(https?://[^""]+)""")) {
            var u = x.Groups[1].Value;
            bool bad = false; foreach (var b in badSites) if (u.ToLower().Contains(b)) bad = true;
            if (!bad) sites.Add(u);
        }
        okCount++;
    } catch (Exception ex) {
        err = ex.Message; errCount++;
    }

    // dedup
    phones = new List<string>(new HashSet<string>(phones));
    emails = new List<string>(new HashSet<string>(emails));
    sites  = new List<string>(new HashSet<string>(sites));

    string row = string.Join(";", new string[] {
        inn, ogrn, Q(name), Q(region),
        Q(string.Join(", ", phones)), Q(string.Join(", ", emails)),
        Q(string.Join(", ", sites)), Q(err)
    });
    File.AppendAllText(outputPath, row + "\r\n", Encoding.UTF8);
    done.Add(inn);
    project.SendInfoToLog(inn + " -> тел:" + phones.Count + " почт:" + emails.Count
                          + " сайт:" + sites.Count + (err.Length > 0 ? " ОШИБКА:" + err : ""), false);

    // вежливая пауза (главное против блокировок)
    System.Threading.Thread.Sleep(rnd.Next(delayMin * 1000, delayMax * 1000));
}

project.SendInfoToLog("Готово: +" + okCount + " новых, ошибок " + errCount, true);
```

## Настройка темпа (чтобы не ловить блок)

- `delayMin=3`, `delayMax=7` — старт. Если пойдут блоки/капча — увеличивай до
  `6`–`12`. Один аккаунт = один вежливый темп; спешка = блокировки.
- Прогон можно останавливать и запускать снова — докачает с места (по `outputPath`).

## Если «ОГРН не найден по ИНН»

Значит адрес строки поиска у checko другой. Открой на checko.ru поиск по любому
ИНН, посмотри URL в адресной строке и замени в коде
`"https://checko.ru/search?query=" + inn` на реальный.

## Импорт списка ИНН из текущей базы

Из `data\list.csv` (столбец ИНН) в `inns.txt`:

```powershell
Import-Csv C:\seostat\Parser2\data\list.csv -Delimiter ';' |
  Select-Object -ExpandProperty 'ИНН' | Set-Content C:\zp\inns.txt -Encoding UTF8
```
