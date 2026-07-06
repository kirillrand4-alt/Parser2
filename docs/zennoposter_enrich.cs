// ============================================================================
//  ZennoPoster 7.9 - дообогащение checko по ИНН (1 аккаунт)
//  Вставь ВЕСЬ этот код в один куб "Свой C# код".
//  ВАЖНО: без строк "using" - в кубе они не работают, поэтому имена типов
//  указаны полностью (System.IO.File и т.п.). Ничего дописывать не нужно.
//
//  Переменные проекта (Данные проекта -> Переменные):
//    inputPath   - файл со списком ИНН (по одному в строке или CSV; 1-я колонка)
//    outputPath  - куда писать результат CSV (создаётся сам; докачиваемо)
//    delayMin    - мин. пауза между ИНН, сек (напр. 3)
//    delayMax    - макс. пауза между ИНН, сек (напр. 7)
// ============================================================================

string inputPath  = project.Variables["inputPath"].Value;
string outputPath = project.Variables["outputPath"].Value;
int delayMin = int.Parse(project.Variables["delayMin"].Value);
int delayMax = int.Parse(project.Variables["delayMax"].Value);
var rnd = new System.Random();

// экранирование CSV-поля
System.Func<string,string> Q = s => {
    if (s == null) return "";
    if (s.Contains(";") || s.Contains("\"") || s.Contains("\n") || s.Contains("\r"))
        return "\"" + s.Replace("\"", "\"\"") + "\"";
    return s;
};

// читаем ИНН (первая колонка строки)
var inns = new System.Collections.Generic.List<string>();
foreach (var line in System.IO.File.ReadAllLines(inputPath, System.Text.Encoding.UTF8)) {
    var s = line.Split(';')[0].Trim();
    if (System.Text.RegularExpressions.Regex.IsMatch(s, @"^\d{10}$|^\d{12}$")) inns.Add(s);
}

// уже обработанные (докачка)
var done = new System.Collections.Generic.HashSet<string>();
if (System.IO.File.Exists(outputPath)) {
    foreach (var line in System.IO.File.ReadAllLines(outputPath, System.Text.Encoding.UTF8)) {
        var s = line.Split(';')[0].Trim();
        if (s.Length > 0) done.Add(s);
    }
} else {
    System.IO.File.WriteAllText(outputPath,
        "ИНН;ОГРН;Название;Регион;Телефоны;Почты;Сайты;Ошибка\r\n", System.Text.Encoding.UTF8);
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
    var phones = new System.Collections.Generic.List<string>();
    var emails = new System.Collections.Generic.List<string>();
    var sites  = new System.Collections.Generic.List<string>();
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
            project.SendInfoToLog("Блок/капча на ИНН " + inn + " - пауза 60 c, реши капчу вручную", true);
            System.Threading.Thread.Sleep(60000);
            html = instance.ActiveTab.DocumentText;
            curUrl = instance.ActiveTab.URL;
        }

        // ОГРН из URL (редирект на карточку) или из первой ссылки на компанию
        var m = System.Text.RegularExpressions.Regex.Match(curUrl, @"-(\d{13})(?:[/?#]|$)");
        if (!m.Success) m = System.Text.RegularExpressions.Regex.Match(html, @"/company/[^""']*?-(\d{13})");
        if (!m.Success) throw new System.Exception("ОГРН не найден по ИНН");
        ogrn = m.Groups[1].Value;

        // 2) карточка по ОГРН (если ещё не на ней)
        if (!curUrl.Contains(ogrn)) {
            instance.ActiveTab.Navigate("https://checko.ru/company/" + ogrn, "");
            instance.ActiveTab.WaitDownloading();
            System.Threading.Thread.Sleep(2500);
            html = instance.ActiveTab.DocumentText;
        }

        // название / регион из <title>
        var t = System.Text.RegularExpressions.Regex.Match(html, @"<title>(.*?)</title>",
                    System.Text.RegularExpressions.RegexOptions.Singleline);
        if (t.Success) {
            var segs = System.Text.RegularExpressions.Regex.Split(t.Groups[1].Value, @"\s[–—-]\s");
            if (segs.Length > 0) name = segs[0].Trim();
            if (segs.Length > 1 && !segs[1].Trim().StartsWith("ИНН")) region = segs[1].Trim();
        }

        // контакты
        foreach (System.Text.RegularExpressions.Match x in
                 System.Text.RegularExpressions.Regex.Matches(html, @"tel:([+\d\s\(\)\-]{6,})"))
            phones.Add(x.Groups[1].Value.Trim());
        foreach (System.Text.RegularExpressions.Match x in
                 System.Text.RegularExpressions.Regex.Matches(html, @"mailto:([^""'?<>]+)")) {
            var e = x.Groups[1].Value.Trim();
            bool bad = false; foreach (var b in badMail) if (e.ToLower().Contains(b)) bad = true;
            if (!bad && e.Contains("@")) emails.Add(e);
        }
        foreach (System.Text.RegularExpressions.Match x in
                 System.Text.RegularExpressions.Regex.Matches(html, @"href=""(https?://[^""]+)""")) {
            var u = x.Groups[1].Value;
            bool bad = false; foreach (var b in badSites) if (u.ToLower().Contains(b)) bad = true;
            if (!bad) sites.Add(u);
        }
        okCount++;
    } catch (System.Exception ex) {
        err = ex.Message; errCount++;
    }

    // dedup
    phones = new System.Collections.Generic.List<string>(new System.Collections.Generic.HashSet<string>(phones));
    emails = new System.Collections.Generic.List<string>(new System.Collections.Generic.HashSet<string>(emails));
    sites  = new System.Collections.Generic.List<string>(new System.Collections.Generic.HashSet<string>(sites));

    string row = string.Join(";", new string[] {
        inn, ogrn, Q(name), Q(region),
        Q(string.Join(", ", phones)), Q(string.Join(", ", emails)),
        Q(string.Join(", ", sites)), Q(err)
    });
    System.IO.File.AppendAllText(outputPath, row + "\r\n", System.Text.Encoding.UTF8);
    done.Add(inn);
    project.SendInfoToLog(inn + " -> тел:" + phones.Count + " почт:" + emails.Count
                          + " сайт:" + sites.Count + (err.Length > 0 ? " ОШИБКА:" + err : ""), false);

    // вежливая пауза (главное против блокировок)
    System.Threading.Thread.Sleep(rnd.Next(delayMin * 1000, delayMax * 1000));
}

project.SendInfoToLog("Готово: +" + okCount + " новых, ошибок " + errCount, true);
