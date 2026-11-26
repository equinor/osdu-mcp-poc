# OSDU Discovery MCP – kom i gang

OSDU Discovery er en MCP-server som gir AI-assistenter (GitHub Copilot, Claude
og andre) oppslag i OSDU. Den svarer på blant annet:

- hvilket API-kall du skal bruke («hvordan ser jeg endringshistorikken til en
  record?»);
- hvilken record-type og hvilken property som holder en verdi («hvor ligger
  dybden til en formasjonstopp?»);
- hvordan records henger sammen («hvordan kommer jeg fra en WellLog til feltet
  den tilhører?»);
- hvilke properties som er utfaset, og hva som erstatter dem.

Den bruker lokale kopier av OSDU sine OpenAPI-specs og JSON-skjemaer
(data-definitions v0.30.1), og av den logiske datamodellen (LinkML). Alt følger
med i repoet. Serveren kjører lokalt og kobler seg ikke til noe OSDU-miljø. Den
trenger ingen innlogging og bygger ingen indeks; den starter på noen få
sekunder.

Repo: <https://github.com/equinor/osdu-mcp-poc>

---

## 1. Installer

Du trenger `git` og [uv](https://docs.astral.sh/uv/), som installerer riktig
Python-versjon selv.

**macOS**

```bash
brew install uv
```

**Windows**

```powershell
winget install --id=astral-sh.uv -e
```

Klon repoet og installer avhengighetene:

```bash
git clone https://github.com/equinor/osdu-mcp-poc.git
cd osdu-mcp-poc
uv sync
```

Sjekk at det virker:

```bash
uv run osdu-query
```

Søket bygges på noen sekunder. Skriv for eksempel `validate legal tags` og
trykk Enter. Skriv `quit` for å avslutte.

## 2. Finn de to stiene du trenger

Klientene starter serveren selv, og trenger fulle (absolutte) stier.

| Hva | macOS | Windows |
|---|---|---|
| Sti til `uv` | `which uv`, ofte `/opt/homebrew/bin/uv` | `where.exe uv`, bruk den første linjen den skriver ut |
| Sti til repoet | `pwd` i repo-mappen, f.eks. `/Users/<deg>/osdu-mcp-poc` | `cd` i repo-mappen, f.eks. `C:\Users\<deg>\osdu-mcp-poc` |

Bruk den fulle stien til `uv`, ikke bare `uv`: klientene finner den ikke
alltid selv.

I eksemplene under står `UV` for stien til `uv` og `REPO` for stien til
repoet. Bytt dem ut med dine egne. På Windows skriver du stiene i JSON med
skråstrek (`C:/Users/deg/osdu-mcp-poc`) eller dobbel omvendt skråstrek
(`C:\\Users\\deg\\osdu-mcp-poc`).

## 3. Koble til klienten

### GitHub Copilot CLI (terminal)

Åpne eller opprett `~/.copilot/mcp-config.json`
(Windows: `%USERPROFILE%\.copilot\mcp-config.json`), og legg inn:

```json
{
  "mcpServers": {
    "osdu-discovery": {
      "type": "local",
      "command": "UV",
      "tools": ["*"],
      "args": [
        "run", "--project", "REPO", "osdu-mcp",
        "--specs", "REPO/specs",
        "--schemas", "REPO/schemas",
        "--model", "REPO/model/osdu_latest.yaml"
      ]
    }
  }
}
```

Har du andre servere i filen fra før, legger du bare `"osdu-discovery": {...}`
inn under den eksisterende `mcpServers`.

Start en **ny** `copilot`-økt og kjør `/mcp`. `osdu-discovery` skal stå som
tilkoblet, med 8 verktøy.

### VS Code (Copilot Chat)

Lag `.vscode/mcp.json` i et arbeidsområde, eller kjør **MCP: Open User
Configuration** fra kommandopaletten for å ha serveren i alle arbeidsområder.
Merk at VS Code bruker `servers` og `"type": "stdio"`:

```json
{
  "servers": {
    "osdu-discovery": {
      "type": "stdio",
      "command": "UV",
      "args": [
        "run", "--project", "REPO", "osdu-mcp",
        "--specs", "REPO/specs",
        "--schemas", "REPO/schemas",
        "--model", "REPO/model/osdu_latest.yaml"
      ]
    }
  }
}
```

Klikk **Start** over serveren i `mcp.json`. Bytt Copilot Chat til
**Agent**-modus, for verktøyene brukes ikke i Ask-modus. Under
verktøyikonet kan du sjekke at verktøyene er slått på.

### Andre klienter

Claude Desktop, Claude Code, Cursor og OpenCode har ferdige eksempler i
repoet (`*.example.json`) og i README-en, under «Client configuration».

## 4. Prøv det

Spør på vanlig norsk eller engelsk, for eksempel:

- *Hvilket OSDU-API bruker jeg for å se endringshistorikken til en record?*
- *Hvilken record-type har formasjonstopper, og hvilken property har dybden?*
- *Hvilke properties på en WellLog peker til en Wellbore?*
- *Hvordan kommer jeg fra en WellLog til feltet den tilhører?*
- *Hvilke properties på Wellbore er utfaset, og hva erstatter dem?*
- *Vis JSON-skjemaet for dataset--File.Generic.*

Første gang et verktøy brukes, ber klienten om lov. Godkjenn det.

**Tips for gode svar**

- **Start en ny økt** fra en vanlig mappe. En gammel, gjenopptatt økt, eller
  en økt startet i en mappe med OSDU-kildekode, gjør at agenten heller går og
  leser kode enn bruker verktøyene, og da tar svaret minutter i stedet for
  sekunder.
- **Be om verktøyene** hvis agenten vandrer: «svar ut fra osdu-discovery-verktøyene».
- **Still spørsmål i fagord.** Du trenger ikke kunne OSDU-navnene;
  agenten slår dem opp i katalogen.

## Verktøyene

| Verktøy | Hva det gjør |
|---|---|
| `list_operations` | Alle API-operasjonene (366), én linje hver, gruppert etter tjeneste |
| `list_kinds` | Alle record-typene (siste versjon), med én linje beskrivelse. Referansedata (kodelister) er skjult som standard |
| `list_properties` | Alle properties på en record-type: sti, type, beskrivelse, koblinger og enhet |
| `search_osdu` | Nøkkelordsøk i operasjoner og properties, best når du kan navnet |
| `get_schema` | Hele JSON-skjemaet for en record-type |
| `get_relationships` | Hva en record-type peker til, og hva som peker til den |
| `find_path` | Hvordan to record-typer henger sammen, steg for steg |
| `get_deprecations` | Utfasede properties og hva som erstatter dem |

## Oppdatering

```bash
cd REPO
git pull
```

Start klienten (eller økten) på nytt etterpå. Ingenting må bygges.

## Feilsøking

| Problem | Løsning |
|---|---|
| Serveren starter ikke, `ENOENT` eller `command not found` | `command` må være full sti til `uv`; se steg 2. |
| Ingen verktøy dukker opp | Start klienten helt på nytt; de fleste leser oppsettet bare ved oppstart. I VS Code: sjekk at du er i Agent-modus. |
| Et verktøy svarer `... unavailable` og nevner `--specs`, `--schemas` eller `--model` | Den stien mangler eller er feil i oppsettet. |
| Serveren avslutter med `ERROR: nothing loaded` | Ingen av stiene `--specs`, `--schemas` eller `--model` fantes. Sjekk `REPO`. |
| Du vil se hva serveren gjør | Kjør kommandoen fra oppsettet direkte i terminalen. En frisk start skriver fire linjer som slutter på `enabled.` eller `ready.`, og venter deretter. Avslutt med Ctrl+C. |

Mer detaljer står i [README.md](../README.md) i repoet.
