# MailArchive 0.0.1 – Neustart mit Quellen, Zeiträumen und zuverlässiger Ablage

Stand: 21. September 2026. Überarbeiteter Architektur- und Umsetzungsplan nach
Codeprüfung und kritischem Austausch mit zwei weiteren GPT-6-Astra-Subagenten
(High Effort). Es gibt keine bestehenden Anwender oder zu unterstützenden
Installationen. Der geplante Produktneustart beginnt bei 0.0.1; frühere
Versionsstände und Datenformate sind keine Kompatibilitätsanforderung.
Dieser Plan beschreibt die nächste Umsetzung, nicht bereits geänderten Anwendungscode.

## 1. Entscheidungen

1. MailArchive startet bei 0.0.1 mit neuem Datenmodell und leerem Datenbestand.
   Frühere Prototypformate, Migrationen, Konfigurations- und Historienimporte sowie
   Versionsübergänge entfallen aus dem Produktumfang.
2. Alle Regeln sind normale, frei löschbare Regeln. Die erste aktive, für das Konto
   passende Regel, deren Bedingungen zutreffen, bestimmt alle Ziele. Keine passende
   Regel bedeutet keine Ablage.
   Eine ausdrücklich leere Regelliste bleibt leer. Neue Profile beginnen leer.
3. Jede Regel hat ein oder mehrere vollständige Ablageziele. Ein globaler
   Archive Folder begrenzt die Pfade nicht mehr.
4. Nachrichtenidentität, Zeitmetadaten, Suchfortschritt, Regelentscheidung und
   tatsächlich abgeschlossene Ausgaben werden getrennt gespeichert.
5. Zeitraumabfragen und das ausdrückliche Anwenden aktueller Regeln auf vorhandene
   Nachrichten gehören bereits zum Kernumfang von 0.0.1.
6. Vollständig angenommene Aufträge halten ihre Rohmail lokal vor, bis ihre Ausgaben
   erledigt oder ausdrücklich abgebrochen sind. Ein ausgefallenes Ziel darf nicht
   dazu führen, dass die Ablage später von einer inzwischen gelöschten Mail abhängt.
7. Konfiguration und Verarbeitungsdaten liegen in einer lokalen SQLite-Datenbank.
   Rohmails offener Aufträge liegen daneben; Zugangsdaten bleiben im Credential-Store.

## 2. Was am bisherigen Design tatsächlich fehlt

Der aktuelle Code unterstützt bereits mehrere Postfächer und mehrere Ordner.
`mail_identity.py` unterscheidet mailboxweite Gmail-/Graph-Identitäten und
ordnerbezogene IMAP-Identitäten. Diese Providerunterschiede sind sachlich nötig.

Die wesentlichen Schwächen liegen an anderen Stellen:

- `RemoteMessage` in `imap_client.py` enthält nur ID und Rohinhalt. Ein
  providerübergreifender Empfangszeitpunkt fehlt im Vertrag und in der Historie.
- `storage._mail_datetime()` verwendet den Date-Header bzw. aktuelle Ortszeit
  für Dateinamen und Datumsordner. Das ersetzt keine zeitlich abfragbare Nachricht.
- `processed_message` und `was_processed()` fassen den Abschluss einer Mail zu
  grob zusammen. Damit lässt sich nicht sinnvoll ausdrücken, dass Ziel A fertig
  ist, B noch fehlt oder B nach einem ausdrücklichen neuen Auftrag ergänzt wird.
- `_processing_owner()` dedupliziert teils über Zugriffskonten hinweg. Zusammen
  mit kontenbezogenen Regeln kann dadurch die Poll-Reihenfolge das Ergebnis bestimmen.
- Der jetzige Ablauf verbindet Quellabruf und Dateiausgabe eng. Ohne lokale
  Rohmail kann ein Teilfehler nach Verschieben/Löschen der Quelle unlösbar werden.
- Alte Migrationspfade tragen zum neuen Modell nichts bei und werden entfernt.

Ein Zeitstempel allein ist ebenfalls kein geeigneter Ersatz für eine Identität:
mehrere Mails können denselben Zeitpunkt tragen; importierte Nachrichten können
alte Datumswerte behalten. MailArchive benötigt sowohl Identität als auch Zeitmetadaten.

## 3. Drei verständliche Arbeitsweisen

| Aktion | Bedeutung |
| --- | --- |
| Neue Nachrichten automatisch beobachten | Vorhandenen Bestand erfassen und überspringen; anschließend neu im gewählten Quellbereich erkannte Nachrichten nach Regeln verarbeiten. |
| Vorhandene Nachrichten ablegen | Quelle, Ordner und „alle“ oder Empfangszeitraum wählen; aktuelle Regeln auf diese Auswahl anwenden und neue Ausgaben ergänzen. |
| Offene Ablagen fortsetzen | Bereits angenommene Aufträge mit ihren gespeicherten Zielen weiter abarbeiten; kein erneutes Auswählen einer Regel. |

Die zweite Aktion deckt Ersteinrichtung, historische Ablage und ausdrückliches
Wiederanwenden nach Regeländerungen ab. Beispiel: Zuerst Ziel A, später A+B.
Derselbe Zeitraumlauf erkennt den Erfolgsbeleg für A und ergänzt B.

„Regel speichern“ startet keinen versteckten historischen Lauf. Eine Mail ohne
passende Regel bleibt ohne Ausgabe; spätere Regeländerungen betreffen sie erst
bei einer ausdrücklichen Bereichsaktion. Bereits geschriebene Dateien werden
bei Regeländerungen weder verschoben noch gelöscht.

Ein Bereichsauftrag zeigt vor Start Quellen, Ordner, Zeitgrenzen/Zeitzone,
Regelstand und Beispielpfade. Er benötigt keine vollständige zweite Downloadrunde
nur für eine Vorschau. Die tatsächlichen Treffer und Ergebnisse erscheinen im Lauf.

## 4. Quellen, Nachrichten und Zeit

### Quellenbesitz

Eine Quelle beschreibt ein adressiertes Postfach mit seiner Ordnerauswahl und
besitzt eine feste lokale `source_id`. Genau ein Zugriffskonto ist zuständig.
Ein Zugriffskonto darf weiterhin mehrere Postfächer bedienen.

Erkennbar doppelte Konfigurationen desselben Providers/Postfachs werden abgewiesen
und verweisen auf die bestehende Quelle. Weitere Ordner werden dort ergänzt;
Mehrfachablagen gehören in die Ziele einer Regel. Ein Austausch von Zugangsdaten
ändert die Quellen-ID nicht. Der Wechsel zu einem anderen tatsächlichen Postfach
legt eine neue Quelle an. Ein ausdrücklicher Wechsel des zuständigen Kontos
ändert die Regelzuordnung nur für künftige Läufe, nicht für gespeicherte Aufträge.

Providerbestätigte Kennungen werden genutzt, soweit vorhanden. Eine Mailbox über
IMAP, Graph und unterschiedliche Aliasadressen lässt sich nicht universell als
identisch erkennen. Bei erkennbarer Überschneidung wird auf eine zweite Quelle
hingewiesen; bewusst getrennte Quellen bleiben getrennte Verarbeitungsbereiche.
Es gibt keine automatische konten-/protokollübergreifende Zusammenführung.

### Nachrichtenidentität und Ordner

| Provider | Identität innerhalb einer Quelle | Zeitbasis für Empfangszeiträume |
| --- | --- | --- |
| Gmail API | Nachrichten-ID; mehrere Labels sind Zugehörigkeiten derselben Nachricht. | `internalDate` |
| Microsoft Graph | ImmutableId; Ordnerzugehörigkeit wird separat geführt. | `receivedDateTime` |
| Generisches IMAP | Ordner + UIDVALIDITY + UID. | `INTERNALDATE` |

Gmails `internalDate` ist bei normalem Empfang die Annahmezeit, kann bei Importen
aber aus dem Absenderdatum stammen. Das ist kein verlässlicher Importzeitpunkt.
Siehe [Gmail-Nachrichtenmodell](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages).

Graph liefert `receivedDateTime` in UTC. ImmutableIds bleiben bei Ordnerwechseln
innerhalb derselben Mailbox erhalten, nicht zwingend beim Wechsel in eine andere
Mailbox oder beim Export/Reimport. Der entsprechende Prefer-Header gehört in alle
relevanten Requests. Siehe [Graph-Nachrichtenmodell](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0)
und [Graph ImmutableIds](https://learn.microsoft.com/en-us/graph/outlook-immutable-id).

IMAP garantiert keine allgemeine ordnerübergreifende Identität. Kopieren,
Verschieben oder eine neue UIDVALIDITY können neue Quellvorkommen erzeugen.
`Message-ID`, gleicher Betreff, Datum oder gleicher Inhalt reichen nicht als
Beweis zum Zusammenführen. Bereits lokal angenommene Aufträge bleiben trotzdem
vom alten Remote-Locator unabhängig. Siehe [IMAP-Identitäten](https://www.rfc-editor.org/rfc/rfc9051.html#section-2.3.1.1).

MailArchive speichert Empfangszeit in UTC mit ihrer Herkunft, Absenderdatum separat,
Ersterkennungszeit sowie Auftrags-/Abschlusszeiten. Ein nachträglich importiertes
altes Dokument darf deshalb ein altes Empfangsdatum und eine neue Ersterkennung
haben. Noch nicht geladene Metadaten bleiben als solche erkennbar.

Fehlende oder ungültige Empfangsmetadaten werden nicht still durch „jetzt“ ersetzt.
Eine davon abhängige Auswahl/Ablage erhält einen sichtbaren Aufnahmefehler.
Ein fehlerhafter Date-Header verhindert dagegen keine Ablage mit gültigem
Provider-Empfangsdatum.

## 5. Zeiträume und automatische Beobachtung sind verschiedene Abfragen

### Vorhandenen Zeitraum bearbeiten

„Januar 2026“ bedeutet: jetzt noch erreichbare Nachrichten mit Empfangsdatum im
Januar in den jetzt ausgewählten Quellordnern. Es bedeutet nicht, dass der historische
Ordnerzustand oder damals vorhandene, inzwischen gelöschte Nachrichten rekonstruiert
werden. Eine Quelle mit mehreren ausgewählten Ordnern liefert deren Vereinigung.

- Der Auftrag hält Quelle, Ordnerauswahl, Regelstand und Zeitzone bei Start fest.
- UI-Tage werden in UTC-Grenzen übersetzt: Beginn inklusive, Ende exklusiv.
  „Bis einschließlich 31. Januar“ endet am lokalen Beginn des 1. Februar.
- Der Provider wird nach dem Bereich gefragt; die lokale Historie allein ist
  keine vollständige Suchgrundlage. Empfangsmetadaten werden exakt nachgeprüft.
- Eigene Pagination/Checkpoints erlauben Fortsetzen nach Fehlern. Sie überschreiben
  niemals den Cursor der automatischen Beobachtung. Ein abgelaufener Seitentoken
  darf einen erneuten Scan derselben Auswahl auslösen; die Ausgabebelege verhindern
  Wiederholungen bereits erledigter Ausgaben derselben Nachrichtenidentität.
- Während einer laufenden Providerabfrage können sich Postfächer ändern. MailArchive
  verspricht keinen transaktionalen historischen Snapshot. Nicht mehr ladbare
  gefundene Nachrichten werden als konkrete Aufnahmefehler berichtet.

Providerseitige Suche ist eine Vorselektion: Gmail verwendet Epoch-Sekunden statt
Datumsstrings mit impliziter PST-Zone; Grenzfälle werden lokal geprüft.
Siehe [Gmail-Suche](https://developers.google.com/workspace/gmail/api/guides/filtering).
Bei IMAP ignorieren `SINCE`/`BEFORE` Uhrzeit und Zeitzone: konservativ überlappende
Tage abfragen und anschließend anhand von `INTERNALDATE` filtern. Siehe
[IMAP SEARCH](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.4.4).
Graph-Zeiträume verwenden die normale Nachrichtenauflistung mit Empfangsfilter;
Live-Delta bleibt ein eigener Synchronisationsweg.

### Neue Quellzugänge beobachten

Die Oberfläche verwendet „Vorhandenen Bestand überspringen; anschließend neue
Nachrichten beobachten“. Während der vollständigen Ersterfassung erscheint
„Wird eingerichtet“. Erst mit konsistent abgeschlossener Baseline wird die Quelle
aktiv. Ein sekundengenauer Start ab dem Mausklick wird nicht versprochen;
während der Ersterfassung eingegangene Mails können zum übersprungenen Bestand
gehören und lassen sich anschließend über eine Bereichsaktion ablegen.

Danach entscheiden Quellereignisse bzw. neue Quell-IDs über Discovery, niemals
`Empfangsdatum > letzter Lauf`. Eine nachträglich importierte alte Mail oder eine
erstmals in den überwachten Bereich verschobene Mail bleibt damit verarbeitbar.
Bereits bekannte abgeschlossene, abgebrochene oder ohne Treffer ausgewertete
Nachrichten starten durch wiederholte Ereignisse keinen neuen Auftrag.

Neue Ordner einer Quelle bekommen ihre eigene Ersterfassung. Vorhandene
Ordner-Cursor und offene Aufträge bleiben erhalten; gewünschter Bestand aus dem
neuen Ordner wird ausdrücklich über eine Bereichsaktion abgelegt.

Ein abgelaufener Gmail-/Graph-Cursor führt zu einem Abgleich mit bekannten IDs,
nicht zum Verlust der bisherigen Ausgabebelege. IMAP-UIDVALIDITY-Wechsel ist
hingegen Identitätsverlust: betroffenen Ordner pausieren, Änderung anzeigen und
Bestandsbehandlung anbieten (neue Baseline oder ausdrücklicher Bereichslauf mit
möglichen zusätzlichen Dateien). Andere Quellen und lokal angenommene Aufträge
laufen weiter. Kein stilles Zusammenführen anhand von Message-ID oder Inhalts-Hash.

## 6. Ein Verarbeitungskern mit klaren Verantwortlichkeiten

| Objekt | Dauerhaft gespeicherte Verantwortung |
| --- | --- |
| Source | Lokale Quellen-ID, Provider-/Postfachbindung, zuständiges Konto und Quellbereich. |
| SourceMessage | Quellschlüssel, letzter bekannter Locator/Zugehörigkeiten, Zeitmetadaten und bisheriger Auswertungszustand. |
| ScanRun | Automatische oder manuelle Auswahl, fester Regelstand, Fortschritt, Fehler und eigene Such-Checkpoints. |
| Intake | Gefundene, noch zu ladende/auszuwertende Nachricht mit zugehörigem Auftrag. |
| ArchivePlan | Genau eine ausgewählte Regel mit vollständigen Zielen, eingefrorenen Pfaden/Zeitwerten und lokaler Rohmail. |
| Output / Receipt | Konkretes Artefakt und Ausgabeziel, geplanter endgültiger Dateipfad, Inhaltshash, Versuchszustand und Erfolgsbeleg. |

Das sind Verantwortlichkeiten innerhalb einer Desktop-Anwendung, keine zusätzlichen
Dienste, kein Message-Broker und kein Event-Sourcing-System.

Der Ablauf:

1. Eine Suchseite liefert Quellreferenzen. Intake-Einträge und zulässiger
   Suchfortschritt werden zusammen in einer kurzen DB-Transaktion gespeichert.
   Ein Cursor darf keine ungesichert übersprungenen Kandidaten verdecken.
2. Rohmail und Metadaten vollständig herunterladen. Noch ungeladene Aufnahmen
   werden unabhängig von später geänderten Auswahlfiltern gezielt nachgeladen,
   soweit der Provider-Locator noch gültig ist. Fehler bleiben sichtbar.
3. Mit dem zum Scan gespeicherten Regelstand genau die erste aktive, kontenpassende
   Regel auswählen, deren Bedingungen zutreffen. Ohne Treffer bleibt ein
   Auswertungsergebnis ohne Ablage. Bei „nur Anhänge“ ohne Anhänge entsteht für
   dieses Ziel keine Datei; weitere Ziele derselben Regel werden entsprechend
   ihrer Optionen ausgeführt. Weder fehlende Anhänge noch Schreibfehler starten
   eine weitere Regelsuche.
4. Benötigte Rohmail dauerhaft im lokalen Arbeitsverzeichnis veröffentlichen;
   dann Plan und konkrete Ausgaben verbindlich anlegen. Erst dieser Zustand heißt
   „angenommen“. Eine bloß bekannte Remote-ID bietet noch keinen Löschschutz.
5. Alle Ziele unabhängig abarbeiten. Erfolge und Fehler einzeln speichern. Ziel B
   darf Ziel A/C sowie die spätere Discovery weiterer Mails nicht blockieren.
6. Nach vollständigem Erfolg bzw. ausdrücklichem Abbruch Rohmail freigeben.
   Der Verlauf und erfolgreiche Ausgabebelege bleiben erhalten.

Automatische und manuelle Aufnahme derselben Quelle werden zunächst bis zur
Aufnahmeentscheidung serialisiert. Zielverarbeitung läuft unabhängig davon.
DB-Constraints sichern zusätzlich: **höchstens eine aktive Intake-Reservierung
oder einen offenen Plan pro Quellnachricht**. Ein Bereichslauf zeigt Nachrichten
mit reservierten Aufnahmen oder offenen Plänen als übersprungen an und plant sie
nicht parallel mit neuen Regeln. Vor Plananlage prüfen automatische Wiederholungen
den terminalen Auswertungszustand erneut, damit ein verzögerter Intake keinen
zweiten Auftrag startet. Erst Abschluss oder ausdrücklicher Abbruch gibt eine
bewusst neue Auswertung frei.

Regeländerungen überschreiben keine offenen Pläne. Pause erhält Plan und Rohmail;
Abbruch beendet offene Arbeit dauerhaft und entfernt keine fertigen Dateien.
Ein späteres Provider-Ereignis startet abgebrochene Arbeit nicht erneut. Ein
bewusst neuer Bereichslauf darf dieselbe Nachricht wieder auswählen.

Abbruch eines Bereichslaufs beendet seine Suche und seine noch nicht angenommenen
Intake-Einträge. Bereits angenommene Pläne bleiben sichtbar und können separat
fortgesetzt oder abgebrochen werden. Die Zuordnung dieser Pläne zum abgebrochenen
Lauf bleibt erkennbar; der Dialog erklärt, welche Arbeit weiter bestehen bleibt.

## 7. Wiederholbare Ausgaben statt eines globalen „bereits verarbeitet“

Eine Ausgabe wird anhand von Quelle/Nachricht, Artefaktvorkommen, dessen
Inhaltsversion und effektiv angefordertem Ziel identifiziert. Regel-ID, Ziel-ID,
Lauf-ID und Layout-Auswahl sind keine eigenständigen Gründe für eine weitere Kopie.

- Zwei Ziele, die nach Auflösung dieselbe Ausgabe verlangen, werden vereinigt.
- Unterschiedliche echte Zielpfade erzeugen beabsichtigte Kopien.
- Zwei gleiche Anhänge innerhalb einer Mail bleiben zwei Vorkommen; ihre
  MIME-Position/Occurrence wird erhalten, auch bei identischem Namen und Inhalt.
- Anhänge erhalten eigene Inhaltshashes. Ein geänderter MIME-Gesamthash allein
  macht bei gleicher nachweisbarer Artefaktzuordnung nicht alle unveränderten
  Anhänge zu neuen Ausgaben. Auch ein Ordner pro Mail verwendet eine stabile
  Archivkennung statt eines bei jedem Download neu gebildeten MIME-Gesamthashes.
- Eine erneut gefundene Mail mit verändertem Inhalt erzeugt keine Überschreibung.
  Geänderte Artefakte werden als neue Inhaltsversion mit eigenem Ausgabebeleg
  behandelt; unveränderte werden bei nachweisbar gleicher Artefaktzuordnung
  wiedererkannt. MIME-Positionen können sich zwischen Inhaltsversionen ändern.
  Bei strukturell veränderter MIME ohne eindeutige Zuordnung werden betroffene
  Artefakte als neue Vorkommen behandelt; eine universelle Zusammenführung wird
  nicht zugesagt.
- Ein eigener Erfolgsbeleg erkennt beim wiederholten Bereichslauf bereits
  geschriebene Ausgaben wieder. A+B nach vorher A ergänzt somit nur B.
- Eine bereits vorhandene gleichnamige Datei gilt nicht allein deshalb
  als eigener Erfolg. Kollisionen werden ohne Überschreiben aufgelöst.
- Die Zielanforderung und der tatsächlich gewählte kollisionsfreie Dateiname
  werden dauerhaft verknüpft, bevor veröffentlicht wird. Temporäre Dateien und
  atomare Veröffentlichung verhindern halbe sichtbare Ausgaben, soweit das
  Dateisystem die benötigten Operationen unterstützt; andernfalls Fehler melden.
- Nach Absturz zwischen Veröffentlichung und DB-Erfolg wird ausschließlich der
  zuvor geplante Pfad anhand des erwarteten Inhalts abgeglichen. Erfolgreiche
  Ausgaben dürfen dabei keine zweite Kopie bekommen.

Diese Wiederholbarkeit gilt innerhalb einer Quelle und ihrer nachweisbaren
Nachrichtenidentität. Sie ist keine globale Gleichsetzung verschiedener Postfächer,
IMAP-Kopien oder Pfad-Aliase derselben Freigabe.

Ein Erfolgsbeleg ist kein dauernder Integritätscheck des Archivs. Wenn der Nutzer
später eine Datei manuell löscht, stellt der normale Bereichslauf sie nicht
automatisch wieder her. Archivprüfung/Reparatur ist nicht Teil des Erstumfangs.

## 8. Lokaler Arbeitsbestand und Wiederaufnahme

Offene angenommene Pläne halten die vollständige Rohmail lokal vor. Deshalb
funktioniert: A geschrieben, B offline, Quellmail danach gelöscht, B später
wieder erreichbar. Dafür sind dann weder Mailserver noch gültiges OAuth-Token nötig.

Der Arbeitsbestand ist kein dauerhaftes Zweitarchiv. Dateien werden nach
Abschluss/Abbruch gelöscht, aber niemals durch eine automatische Ablaufzeit,
solange ein Plan sie noch benötigt. Bei „kein Treffer“ wird Rohinhalt ebenfalls
freigegeben. Ein späterer neuer Bereichslauf lädt benötigte Inhalte erneut.

- Aufnahme streamt in temporäre Dateien und prüft Kapazität; Speichergrenze und
  freier Plattenplatz begrenzen neue Downloads. Eine Reserve für DB-Statusupdates
  verhindert, dass die Aufnahme selbst den gesamten Datenträger belegt. Ein
  einzelnes zu großes Objekt erhält einen sichtbaren Fehler statt einer Endlosschleife.
- An der Aufnahmegrenze pausieren und melden, während vorhandene Zielaufträge
  weiterarbeiten und Platz freigeben können. Füllen andere Prozesse den
  Datenträger vollständig, bei unmöglichen Statusupdates sicher pausieren, bis
  Platz frei ist. Keine stille Verwerfung und keine unbegrenzt weiterwachsende
  Discovery-Warteschlange.
- Neustart bereinigt verwaiste temporäre Dateien, erhält alle referenzierten
  Rohmails und setzt unterbrochene Versuche in einen wiederaufnehmbaren Zustand.
- Fehlende/beschädigte referenzierte Rohmail ist ein sichtbarer Fehler. Kein
  erfolgreiches Abschließen und keine stille Neubewertung nach aktuellen Regeln.
- Lokale Dateien erhalten benutzerbezogene Zugriffsrechte; Größe und offene
  Aufträge sind im UI sichtbar. Löschaktionen zeigen, welche Arbeit sie beenden.

SQLite, lokale Rohmails und externe Zieldateien bilden keine gemeinsame atomare
Transaktion. Die Veröffentlichungs-/Wiederaufnahmezustände sind deshalb explizit
zu testen; die gemeinsame DB ersetzt diese Protokolle nicht.

## 9. Ablageziele und Oberfläche

Ein Regelziel enthält eine stabile ID, vollständigen Pfad bzw. Pfadvorlage,
Save-Modus (E-Mail, Anhänge, beides) und Anhanglayout (direkt oder pro Mail).
Alle Ziele sind innerhalb der einen Regel gemeinsam sichtbar und bearbeitbar.

- Absolute lokale und UNC-Pfade bzw. bereits eingebundene Dateisystempfade.
- Fehlende Zielunterordner rekursiv anlegen, sofern das Betriebssystem dies erlaubt.
- `{year}` und `{month}` verwenden standardmäßig das Provider-Empfangsdatum
  in der konfigurierten Ablagezeitzone. Zeitpunkt und Zeitzone werden im Plan
  eingefroren. Vorschau und Schreiben verwenden denselben Resolver.
- Literale Platzhalterzeichen erhalten eine eindeutige Escape-Schreibweise.
  Benutzerdefinierte Pfade werden nicht still umbenannt; Anhangnamen bleiben
  gesondert gegen ungültige Namen und Pfadausbruch abgesichert.
- Datei- und Verzeichnisfehler erscheinen am konkreten Ziel. Retries haben
  Warteabstände; ein offline Ziel hält gesunde Ziele nicht auf.
- Der bisherige globale „Archiv öffnen“-Knopf öffnet die Zielauswahl bzw. ein
  konkretes Ziel aus Regel oder Verlauf.
- „Folders / label IDs“ erhält Scrollbalken, Resize und Maus-/Touchpad-Bedienung.
  Ein Name pro Zeile bleibt erhalten. Einfache/mehrfache innere Leerzeichen werden
  beim Speichern, Laden, Providerzugriff und in der Identität nicht verändert.
- Verlauf trennt Aufnahmefehler von Ausgabefehlern und zeigt gewählte Regel,
  Empfangsdatum, Quelle und den Stand je Ziel. „2 von 3 Zielen“ ist kein Gesamterfolg.

### Grenze bei Netzlaufwerken

Mit „Freigabe“ war ein als Pfad erreichbarer Netzwerk-Speicher gemeint, keine
Berechtigung. Beispiel: Ein NAS ist unter `/mnt/nas` eingebunden. Nach dem
Aushängen kann dort ein normaler lokaler Ordner verbleiben; ein Schreibzugriff
könnte dann lokal erfolgreich sein. Das ist der spezielle Fall hinter dem Reviewpunkt.

MailArchive baut dafür keinen Netzwerk-/Internetmonitor, keine Mountverwaltung und keinen
Hintergrunddienst. Es schreibt an den vom Betriebssystem bereitgestellten Pfad
und behandelt tatsächliche Schreibfehler. Ein bloßer Existenztest eines Ordners
wird nicht als Schutz verkauft. Die Betriebssystemeinbindung und Berechtigungen
müssen bei solchen Mounts verhindern, dass unbeabsichtigt lokal weitergeschrieben
werden kann. Eine zusätzliche Zielidentitätsprüfung per Kennungsdatei gehört
nicht zum Erstumfang. Die Anwendung garantiert keine Erkennung eines falsch
oder nicht eingebundenen Mediums hinter einem trotzdem beschreibbaren Pfad.

## 10. Frische Einrichtung und Persistenz

MailArchive verwendet ein lokales Profil im plattformüblichen Benutzer-
Datenverzeichnis. Darin liegen `workspace.sqlite3`, Arbeitsdateien und Logs.
Eine Single-Instance-Sperre verhindert gleichzeitigen Betrieb mehrerer Instanzen.
Die erste Einrichtung beginnt ohne Quellen, Regeln oder aktive Verarbeitung.
Zugänge werden ausdrücklich eingerichtet; Zugangsdaten liegen im Credential-Store.
Ein besonderer Versionsnamensraum wie `v2` ist nicht erforderlich.

Es gibt keine Übernahme bisheriger Prototypkonfigurationen, kein Zusammenführen
alter Historien und keinen Migrations-/Rückmigrationsassistenten. Entwicklungs-
und Testbestände werden bei Bedarf ausdrücklich neu angelegt. Vorhandene
Archivdateien sind normale Zieldateien: Sie werden nicht überschrieben und gelten
ohne eigenen Erfolgsbeleg nicht automatisch als bereits erledigte Ausgaben.

Das neue Format erhält ab Beginn eine eindeutige Schema-Kennung. Eine unbekannte
oder beschädigte Datenbank wird abgewiesen statt still überschrieben. Das ist
Formatvalidierung, keine Verpflichtung, frühere Prototypen zu unterstützen.
Konfiguration wird als
versioniertes JSON-Dokument IN SQLite gespeichert; bestehende Settings-Objekte
und eine `load/save`-Fassade können erhalten bleiben. Quellidentitäten und
Laufzeitdaten werden relational abgesichert. Aktive Regeln werden nicht zusätzlich
als zweite unabhängig gepflegte Wahrheit gespeichert. Ein Lauf referenziert seine
Konfigurationsrevision; benötigte historische Stände bleiben erhalten.

Kurze Transaktionen verbinden etwa Laufanlage und Konfigurationsstand oder
Discovery-Fortschritt und Intake. Netzwerk-/Dateizugriffe erfolgen außerhalb von
DB-Schreibtransaktionen. Kein neues ORM oder Ereignisarchiv notwendig.

Die Datenbank und der Rohmail-Arbeitsbestand bleiben lokal. Frei wählbare Netzpfade
sind Ablageziele. Live-Datenbankwechsel, History-Merge und Export-/Importframework
entfallen aus 0.0.1. Ein Profilbackup braucht konsistente DB plus referenzierte
Arbeitsdateien; Zugangsdaten bleiben separat geschützt. Ein künftiger Upgradepfad
wird erst für tatsächlich veröffentlichte und genutzte Datenformate festgelegt.

## 11. Umsetzung in prüfbaren Paketen

| Paket | Ergebnis und Abnahme |
| --- | --- |
| 1. Verträge und neuer Datenbestand | Quellenbesitz, Provider-Zeitmodell, ein lokaler Store, leere Defaults und Zustandsübergänge; alte Formatkonverter entfernen. |
| 2. Verarbeitungskern | Intake, dauerhafte Rohmail, eingefrorene Erstregel, mehrere Ausgaben, Erfolgsbelege, Crash-Wiederaufnahme, Pause/Abbruch; zunächst mit lokalen Provider-Fakes. |
| 3. Provider und Zeiträume | Metadatenabruf, manuelle Bereiche, Baseline und Live-Cursor sauber getrennt; Providergrenzen und Identitätswechsel getestet. |
| 4. Oberfläche | Mehrfachziele, Bereichsauswahl, offene Aufträge/Platzverbrauch, verständlicher Verlauf, Scrollfeld und Leerzeichen. |
| 5. Paketierung und Pilot | Frische Windows-/Linux-Einrichtung und repräsentativer Kundenablauf bestehen; Versionsangaben und Release-Dokumentation starten konsistent bei 0.0.1. |

Bestehende Parser-, OAuth-, sichere Datei-Veröffentlichungs- und Desktop-Bausteine
werden soweit passend weiterverwendet. Neu strukturiert werden Providervertrag,
Verarbeitung und Persistenz. Bisherige Formatkonverter, zugehörige Migrationstests
und überholte Upgrade-Dokumentation werden entfernt; fachlich weiterhin gültige
Regressionen bleiben erhalten. Die Versionsangaben in Paketmetadaten, Anwendung,
Builds und Dokumentation werden bei der Umsetzung gemeinsam auf 0.0.1 gesetzt.

Der Kern braucht keine zusätzliche Infrastruktur. Neue Module für Profilstore,
Suchläufe, Aufnahme und Ausgabe sind klare Verantwortlichkeiten im bestehenden
Python-Prozess. Zeiträume werden nicht auf ein späteres Release vertagt.

### Veröffentlichung des Neustarts

Der Nutzer entfernt die bisherigen GitHub-Releases. Der erste neue Release-Tag
ist `v0.0.1`; Paketmetadaten, Anwendung und erzeugte Download-Dateien müssen dazu
passen. Eine Übernahme früherer Installationen wird dafür nicht eingeführt.

Für genau diesen ersten Release werden eigene Neustart-Release-Notes verwendet.
Der bisherige Workflow ermittelt seine Changelog-Basis mit `git describe` aus
Git-Tags. Gelöschte GitHub-Releases ändern diese Auswahl nicht: Ein verbleibender
Tag wie `v1.0.4` darf nicht zur Überschrift „Changes since v1.0.4“ für 0.0.1 führen.
Nachfolgende Releases können wieder normale Änderungen seit dem Vorgänger zeigen.
Nach Veröffentlichung werden der öffentliche Latest-Link, die Update-Abfrage
und die angebotenen Windows-/Linux-Dateien auf `v0.0.1` geprüft.

Git-Tags/Quellcodehistorie und Actions-Build-Artefakte sind von Release-Downloads
getrennt. Release-Löschung allein entfernt sie nicht; daraus entsteht keine Pflicht
zum Umschreiben der Git-Historie. Der aktuelle Workflow bewahrt Build-Artefakte
14 Tage auf. Falls auch diese alten Binärdownloads sofort verschwinden sollen,
sind sie separat zu entfernen. Alte Tag-Pushes/Workflow-Wiederholungen werden
nicht zur erneuten Veröffentlichung der verworfenen Prototypen verwendet.

## 12. Verbindliche Ende-zu-Ende-Abnahmen

| Fall | Erwartetes Ergebnis |
| --- | --- |
| Eine Regel, drei beliebige Ziele | Alle verlangten Dateien an A/B/C; fehlende Unterordner werden angelegt. |
| Zwei passende Regeln | Nur erste aktive kontenpassende Regel wirkt, mit allen ihren Zielen. |
| Keine Regeln / kein Treffer | Keine Datei; leere Liste bleibt nach Neustart leer. |
| A erfolgreich, B offline, Quellmail gelöscht | Nach vollständiger lokaler Annahme B später ohne Providerzugriff fertigstellen. |
| Quelle verschwindet vor vollständiger Annahme | Konkreter Aufnahmefehler; kein behaupteter Schutz oder Gesamterfolg. |
| Absturz nach Schreiben vor Erfolgsbeleg | Geplante Datei erkennen, keine zweite Kopie und kein Überschreiben. |
| Regel A wird A+B, gleicher Zeitraum erneut | A anhand Erfolgsbeleg überspringen, B ergänzen. |
| Reservierter Intake/offener alter Plan, Regel geändert, neuer Bereichslauf | Reservierte/offene Arbeit anzeigen/überspringen; keine parallele widersprüchliche Zielmenge oder verzögerte automatische Zweitauswertung. |
| Plan ausdrücklich abgebrochen | Kein Neustart durch Polling; neue Bereichsaktion darf neu auswerten; fertige Dateien bleiben. |
| Bereichslauf mit ungeladenen und bereits angenommenen Mails abgebrochen | Keine neuen Aufnahmen/Pläne aus diesem Lauf; angenommene Pläne bleiben sichtbar und separat steuerbar. |
| Zwei Anhänge mit gleichem Namen und Inhalt | Zwei getrennte Vorkommen erhalten; Wiederholung erzeugt keine weiteren. |
| Zwei Ziele ergeben dieselbe konkrete Ausgabe | Nur eine Ausgabe; unterschiedliche echte Pfade erhalten beide Kopien. |
| Datum an Intervall-/Sommerzeitgrenze | Präzise UTC-Nachprüfung und dokumentierte lokale Tagesgrenzen. |
| Import einer alten Mail nach aktiver Baseline | Neu erkannt trotz altem Empfangsdatum; Datum wird nicht als Cursor missbraucht. |
| Manueller Zeitraum während Automatik | Keine Cursorverfälschung und keine doppelten Pläne/Ausgaben. |
| Gmail mehrere Labels / Graph Ordnerwechsel | Gleiche Quellidentität; nach Annahme unabhängig von Quellzugehörigkeit. |
| IMAP UIDVALIDITY wechselt | Betroffener Scope pausiert/erklärt; keine erfundene Zuordnung zu alten Erfolgen. |
| Spoolgrenze / Platte voll | Aufnahme stoppt mit DB-Reserve, Ausgaben können Platz freigeben; bei extern vollständig gefüllter Platte sicher pausieren bis wieder Platz frei ist. |
| Neue Ordnerauswahl | Eigene Baseline, bestehende Überwachung und offene Aufträge bleiben erhalten. |
| Viele Ordner mit Leerzeichen | Scroll-/Maus-/Tastaturbedienung; Namen über UI, Persistenz und Provider unverändert. |
| Frische Einrichtung | Leerer neuer Datenbestand; keine automatisch ergänzten Regeln und kein Abruf vor ausdrücklicher Aktivierung. |

## 13. Bewusste Grenzen

MailArchive ist ein regelbasiertes Ablagewerkzeug, kein vollständiger Mailclient oder
lückenloses historisches Postfachbackup. Der Erstumfang enthält keine globale
Inhaltsdeduplizierung, Rekonstruktion gelöschter Remote-Mails vor Annahme,
automatische Reparatur extern veränderter Archivdateien, Mountverwaltung,
verteilte Datenbank, Import früherer Prototypformate oder Rückmigration.

Die Behauptung „MailArchive kann dieselbe Mail in jedem Provider, Ordner und Protokoll immer
wiedererkennen“ wäre falsch. Belastbar ist: bekannte Quellidentitäten werden
korrekt behandelt; lokal angenommene Pläne sind wiederaufnehmbar; explizite
Zeitraumläufe werden nicht von einem pauschalen Mail-Abschlussblocker verhindert.
