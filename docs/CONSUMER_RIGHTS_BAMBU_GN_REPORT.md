# Who Controls Your Printer? Consumer Device Rights, the Bambu Lab Backlash, the Gamers Nexus / SFC Fight, and Where `beambam` Stands

*A citation-verified analysis of device-ownership rights, the 2025 Bambu Lab "authorization control" firmware controversy, the 2026 Gamers Nexus / Louis Rossmann / Software Freedom Conservancy legal flashpoint, and how this repository (`beambam`/`x2d`) maps against Bambu Lab's Terms of Use and the AGPL.*

Compiled 2026-06-15.

---

## Methodology & confidence

The external claims in Parts 1–3 and 5 were gathered by a multi-source deep-research pass (parallel web search → primary-source fetch → 3-vote adversarial verification → synthesis), then key documents were re-fetched directly to extract verbatim quotes. **Confidence tags:** *Verified* = confirmed against the cited primary/secondary source in this session; *Repo-verified* = read directly from this source tree; *Established law* = bedrock statute/precedent cited for completeness (not independently re-litigated here). A short list of genuinely open items is at the end.

Government primary URLs (supremecourt.gov, congress.gov, federalregister.gov) frequently return HTTP 403 to automated fetchers; where that happened the text was confirmed via authoritative mirrors (Cornell LII, the Federal Register machine API, official legislature PDFs) and is cited as such.

---

## Executive summary

1. **You own the hardware; the manufacturer treats the software as licensed.** That split — plus DMCA §1201 anti-circumvention — is the lever locked-down device vendors pull. US and EU law has been moving *toward* owners (Right-to-Repair, a 2024 DMCA repair exemption, *Van Buren*, *Google v. Oracle*), but with sharp limits exactly where firmware locks live.
2. **On January 16, 2025 Bambu Lab shipped "authorization control" firmware** that cryptographically gates printer connection and control — including *starting a print*, even in LAN mode — and **prohibits unauthorized third-party software** from critical operations, routing third-party tools through a new "Bambu Connect" client. *(Verified — Bambu blog.)*
3. **The "security" rationale was undercut within days:** the Bambu Connect app's X.509 certificate and private key were extracted on January 19, 2025 because they shipped in plaintext inside an Electron app. *(Verified — Hackaday.)*
4. **The drama escalated dramatically in May 2026.** Bambu pressured developer Paweł Jarczak over his `OrcaSlicer-bambulab` fork (which re-attached Orca to Bambu's cloud), invoking "impersonation" / "falsified identity metadata"; Jarczak pulled the repo; **Gamers Nexus and Louis Rossmann re-hosted it, each pledged $10,000 for his legal defense, and dared Bambu to sue.** *(Verified — GN, Bambu blog, Tom's Hardware.)*
5. **The Software Freedom Conservancy then found Bambu in violation of the AGPLv3** — for withholding the corresponding source of its proprietary networking libraries (`libbambu_networking.so`/`.dll`/`.dylib`) and for imposing "further restrictions" via the C&D — and launched the funded **`baltobu`** project to reverse-engineer those libraries and maintain open forks. **Bambu backtracked.** *(Verified — SFC, Notebookcheck.)*
6. **`beambam` (this repo) is a parallel of exactly what SFC's `baltobu` set out to do:** an MIT-licensed pure-LAN client that reimplements Bambu's networking/control plane. It uniquely starts a print on authorization-control firmware over pure LAN with **no cloud and no Developer Mode**, by recovering a per-installation signing key from a Bambu Handy install and RSA-encrypting the file location to the printer's device certificate. *(Repo-verified.)*
7. **The law is genuinely split on the riskiest pieces.** Reverse-engineering-for-interoperability and LAN-only self-control of your own device are strongly protected (*Sega*, *Connectix*, *Google v. Oracle*, §1201(f), *Van Buren*). But **circumventing the signing TPM** sits in §1201's gray zone, and **distributing a circumvention tool** is the single highest-risk act — protected by *no* triennial exemption. Bambu's Terms of Use §3.4 also purports to forbid reverse engineering outright — a clause the SFC says is itself an unlawful "further restriction" on AGPL rights.

---

# Part 1 — Consumer rights to use, repair, and control devices you own

## 1.1 Ownership vs. licensing (Established law)

You own the **physical object** under the **First Sale Doctrine**, *17 U.S.C. §109(a)* — you may use, resell, or dispose of that copy. Manufacturers structure the **firmware** as a *license* so you never acquire the rights to copy/modify it; under ***Vernor v. Autodesk***, 621 F.3d 1102 (9th Cir. 2010), a user is a *licensee* (not owner) when the vendor labels it a license, restricts transfer, and imposes use restrictions — which, if met, defeats the *17 U.S.C. §117* software-owner self-help rights. *Vernor* binds only the 9th Circuit; the doctrine is **unsettled** elsewhere.
- Sources: [17 U.S.C. §109](https://www.law.cornell.edu/uscode/text/17/109), [17 U.S.C. §117](https://www.law.cornell.edu/uscode/text/17/117).

## 1.2 DMCA §1201 anti-circumvention + the 2024 repair exemption (Verified)

*17 U.S.C. §1201* bans (a)(1)(A) the **act** of circumventing an **access**-control TPM; and (a)(2)/(b)(1) **trafficking** in circumvention **tools**. It also carries permanent exceptions including **§1201(f) reverse engineering for interoperability** and **§1201(j) security testing**.
- **The triennial process — §1201(a)(1)(B)–(D):** every three years the **Librarian of Congress, on the Register of Copyrights' recommendation,** adopts temporary exemptions to the *act* ban.
- **The Ninth Triennial final rule** (FR Doc. **2024-24563**, **89 FR 85437**) became **effective October 28, 2024** and runs to **October 28, 2027**. It grants an exemption at **37 CFR 201.40(b)(15)** for:
  > "Computer programs that are contained in and control the functioning of a lawfully acquired device that is primarily designed for use by consumers, when circumvention is a necessary step to allow the diagnosis, maintenance, or repair of such a device, and is not accomplished for the purpose of gaining access to other copyrighted works."
- **Critical limits:** the exemption covers an **owner circumventing for repair/maintenance/diagnosis of their own device** — it does **not** cover modification beyond repair, and it does **not** cover **distributing** circumvention tools (the anti-trafficking provisions §1201(a)(2)/(b) are untouched, and no exemption ever reaches them). No exemption squarely names "3D-printer firmware."
- Sources: [Federal Register — final rule 2024-24563](https://www.federalregister.gov/documents/2024/10/28/2024-24563/exemption-to-prohibition-on-circumvention-of-copyright-protection-systems-for-access-control), [17 U.S.C. §1201 (Cornell LII)](https://www.law.cornell.edu/uscode/text/17/1201), [Copyright Office 1201/2024](https://www.copyright.gov/1201/2024/).

## 1.3 Right-to-Repair laws (Verified)

At least **five US states** have enacted electronics Right-to-Repair laws requiring makers to provide parts, tools, and documentation on "fair and reasonable terms":

| State | Law | Effective | Note |
|---|---|---|---|
| New York | Digital Fair Repair Act | **Dec 28, 2023** | First US electronics R2R law; covers phones, tablets, computers, **printers**, TVs, cameras |
| California | **SB 244** (Right to Repair Act) | **July 1, 2024** | Approved by Newsom Oct 10, 2023; parts/tools/docs for 3–7 yrs |
| Minnesota | Digital Fair Repair Act | **July 1, 2024** | — |
| Colorado | **HB24-1121** | **Jan 1, 2026** | Includes a parts-pairing restriction (signed May 2024) |
| Oregon | **SB 1596** | law **Jan 1, 2025**; enforcement **July 1, 2027** | **First US law to restrict "parts pairing"** (signed Mar 2024) |

**Oregon SB 1596** defines parts pairing as *"a manufacturer's practice of using software to identify component parts through a unique identifier"* and bars OEMs from using it to (A) prevent/inhibit installing an otherwise-functional replacement part, (B) reduce functionality/performance, or (C) display non-dismissible misleading "unidentified part" alerts. This is the provision most analogous to Bambu's signed-component / authorized-software gating.
- Sources: [Wiley — State R2R patchwork](https://www.wiley.law/alert-State-Right-to-Repair-Patchwork-Grows-as-Electronic-Device-Manufacturers-Face-New-Compliance-Deadlines), [PIRG — State of Right to Repair](https://pirg.org/edfund/resources/the-state-of-right-to-repair/), [CA SB 244 (leginfo)](https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202320240SB244), [Oregon SB 1596 enacted text (PDF)](https://www.oregonlegislature.gov/bills_laws/lawsstatutes/2024orLaw0069.pdf), [LegiScan SB 1596](https://legiscan.com/OR/text/SB1596/id/2932996).
- **EU:** the Right to Repair Directive (EU) 2024/1799 (2024) adds repairability and anti-impeding-repair obligations EU-wide. Source: [EUR-Lex summary](https://eur-lex.europa.eu/EN/legal-content/summary/common-rules-promoting-the-repair-of-goods-and-amending-related-eu-legislation.html).

## 1.4 Magnuson-Moss Warranty Act — anti-tying (Verified)

The FTC has **enforced** the Magnuson-Moss anti-tying rule: in 2022 it acted against **Weber-Stephen**, **MWE Investments (Westinghouse)**, and **Harley-Davidson** for voiding warranties when customers used third-party parts/independent repair — *"void if customers used independent repairers or third-party parts"* — with final orders approved **October 2022** (announced June–July 2022). Conditioning warranty coverage on brand-name parts/service violates the MMWA's anti-tying provision (*15 U.S.C. §2302(c)*). A maker cannot lawfully void a warranty merely because an owner ran third-party parts/software.
- Sources: [PIRG](https://pirg.org/edfund/resources/the-state-of-right-to-repair/), [FTC](https://www.ftc.gov/business-guidance/blog/2019/03/nixing-fix-warranties-mag-moss-restrictions-repairs).

## 1.5 Reverse engineering for interoperability (Verified + Established law)

- ***Google LLC v. Oracle America***, decided **April 5, 2021 (6-2)**: copying the Java API **declaring code** for interoperability is **fair use**. The Court *assumed without deciding* copyrightability and resolved on fair use, treating declaring code's functional nature and role as a learned standard interface — "further than … most computer programs … from the core of copyright" — as favoring fair use. *(Verified.)*
  - Sources: [CRS LSB10597](https://www.congress.gov/crs-product/LSB10597), [SCOTUS opinion 18-956 (PDF)](https://www.supremecourt.gov/opinions/20pdf/18-956_d18f.pdf).
- ***Sega Enterprises v. Accolade***, 977 F.2d 1510 (9th Cir. 1992) and ***Sony Computer Entertainment v. Connectix***, 203 F.3d 596 (9th Cir. 2000): intermediate copying to reverse-engineer for interoperability is **fair use**. *(Established law — the bedrock interoperability line consistent with Google v. Oracle.)*
- **DMCA §1201(f)** codifies a reverse-engineering-for-interoperability exception for someone who lawfully obtained the program.

**Conditions to stay protected:** lawfully own the device/software; copy only what's needed to reach functional/interface elements; **don't redistribute the firmware**; build something independently created. **Overlay risk:** anti-RE clauses in a EULA/ToS may bind by *contract* even where copyright would permit (enforceability contested — and see SFC's AGPL §10 argument in Part 5.4).

## 1.6 CFAA after *Van Buren* (Verified)

***Van Buren v. United States***, No. 19-783, decided **June 3, 2021 (6-3)**: a person "exceeds authorized access" only by obtaining information in **areas of a computer that are off-limits** to them — a **"gates-up-or-down"** inquiry keyed to *whether* access is permitted, **not** the *purpose*. The Court held Congress's removal of the 1984 Act's "purpose" reference "cuts against" a purpose-based reading.
> "An individual exceeds authorized access when he accesses a computer with authorization but then obtains information located in particular areas of the computer—such as files, folders, or databases—that are off-limits to him."
- **Implication:** controlling **your own LAN device** is the safest posture — you are the authorized party, there is no gate to breach. **Merely violating a ToS is unlikely a CFAA crime** post-*Van Buren* — though a ToS can still bind you by **contract** (cf. *hiQ v. LinkedIn*, where scraping public data wasn't CFAA but the defendant still lost on breach of contract). Risk concentrates when you touch the **vendor's cloud**.
- Sources: [SCOTUS opinion 19-783 (PDF)](https://www.supremecourt.gov/opinions/20pdf/19-783_k53l.pdf), [Cornell LII 19-783](https://www.law.cornell.edu/supremecourt/text/19-783), [CRS LSB10616](https://www.congress.gov/crs-product/LSB10616).

## 1.7 Synthesis for 3D printers

| Owner action | Footing | Confidence |
|---|---|---|
| Own / resell / physically repair | First Sale | High |
| Circumvent an access TPM **to repair/diagnose your own device** | 2024 DMCA exemption 37 CFR 201.40(b)(15) | High (until Oct 2027; repair-scoped) |
| Reverse-engineer the LAN protocol for interoperability | *Sega/Connectix/Google* + §1201(f) | High (with conditions) |
| Third-party client controlling **your own** printer on **your own** LAN | Strong; *Van Buren* | High |
| Third-party parts/software vs. warranty | Magnuson-Moss §2302(c) | High |
| **Circumvent the signing/authorization TPM for non-repair control** | §1201(a) gray zone; §1201(f) *may* cover | **Contested** |
| **Distribute a tool that circumvents the TPM** | Anti-trafficking §§1201(a)(2)/(b); **no** exemption | **Highest risk** |
| Violate Bambu ToS via unofficial client | Not CFAA post-*Van Buren*; **breach of contract** | Medium |
| Access Bambu **cloud** via reversed endpoints | Residual CFAA + contract | Medium-risk |

---

# Part 2 — The Bambu "authorization control" controversy (January 2025)

## 2.1 What shipped (Verified)

On **January 16, 2025** Bambu Lab announced a firmware-level *"authorization and authentication protection mechanism for the connection and control of Bambu Lab 3D printers"* (post updated Jan 17 & 20). Beta firmware **01.08.03.00** shipped **Jan 17, 2025**; full release followed in **late January** *(this repo's `docs/COMPARISON.md` records the full build as `01.08.05.00`)*. Gated operations include:
> "Initiating a print job (via LAN or cloud mode). Controlling motion system, temperature, fans, AMS settings, calibrations, etc." — plus binding/unbinding, remote video, and firmware upgrades.

And critically:
> "Unauthorized third-party software will be prohibited from executing critical operations." … third-party slicers "will no longer be able to utilize Studio network plugin API for authorization control."

**Authorization applies even in LAN mode**, on Bambu's stated rationale:
> "even when the printer is in LAN mode, the network environment in which the printer is located may still be connected to the public network, and other malicious software may still be able to remotely access the printer."

- Sources: [Bambu blog — authorization control](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/), [Hackaday 2025-01-17](https://hackaday.com/2025/01/17/new-bambu-lab-firmware-update-adds-mandatory-authorization-control-system/), [3D Printing Industry](https://3dprintingindustry.com/news/bambu-lab-controversy-deepens-firmware-update-sparks-backlash-240588/), [Consumer Rights Wiki — Authorization Control System](https://consumerrights.wiki/w/Bambu_Lab_Authorization_Control_System), [HN discussion](https://news.ycombinator.com/item?id=42764277).

**Technical detail (Repo-verified, `docs/COMPARISON.md`):** the printer verifies every MQTT control command and rejects anything unsigned or signed by an unrecognized cert (`MQTT command verification failed`, HMS `0500-0500-0001-0007`). Per `Doridian/OpenBambuAPI` it requires an **RSA-SHA256 envelope signed by a per-device cert chaining to the Bambu CA with a CN matching the printer's serial** — so a single shared/leaked cert cannot satisfy `print.*`.

## 2.2 "Bambu Connect," the sanctioned replacement (Verified)

Bambu introduced **Bambu Connect** as the official replacement for the Studio network-plugin API: it *"securely transmits sliced Bambu Lab G-code and 3MF files to your printer"* — a transmission/control conduit, not a slicer. Source: [Bambu blog](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/), [Bambu wiki — Bambu Connect](https://wiki.bambulab.com/en/software/bambu-connect).

## 2.3 The "security" rationale collapses in 3 days (Verified)

On **January 19, 2025**, Hackaday reported **"Bambu Connect's Authentication X.509 Certificate And Private Key Extracted"** — a user **[hWuxH]** de-obfuscated the Electron app's `main.js`, found the cert and **private key in plaintext**, and posted it publicly.
> "These are used to encrypt HTTP traffic with the printer, and is the sole thing standing in the way of tools like OrcaSlicer talking with authentication-enabled Bambu Lab printers."

The reporting concluded *"security through obfuscation is not going to be very effective here."* A single, publicly distributed private key being the basis of an advertised "security" feature is the core community grievance.
- Source: [Hackaday 2025-01-19](https://hackaday.com/2025/01/19/bambu-connects-authentication-x-509-certificate-and-private-key-extracted/).

## 2.4 Custom firmware, LAN-only, and backlash (Verified)

- **X1Plus** — an open-source custom firmware for Bambu printers; it tells the auto-update mechanism the device runs version *"99 or higher"* to prevent official firmware from overwriting it. Installation **voids warranty**. *(Consumer Rights Wiki.)*
- **Full local access** can be retained by running LAN-only **without cloud** — but on the new firmware that practically means **staying on / reverting to older firmware**. *(Consumer Rights Wiki.)*
- **Backlash:** customer reaction on forums and Reddit was "negative"; Bambu's Trustpilot page recorded *"a wave of one-star reviews"* citing the restrictions.
- Source: [Consumer Rights Wiki — Authorization Control System](https://consumerrights.wiki/w/Bambu_Lab_Authorization_Control_System).

> **Open item:** the precise terms/timeline of any official "Developer Mode / LAN-only" *walkback* after the Jan 2025 backlash were not pinned to a single primary source in this pass. The repo and ecosystem treat **Developer LAN Mode** — which by design **severs Bambu Cloud and disables auth verification** — as the sanctioned local-control path (`docs/COMPARISON.md:16-23`); confirm Bambu's exact announcement wording before quoting it.

---

# Part 3 — The Gamers Nexus / Louis Rossmann flashpoint (May 2026)

> The flashpoint is **a cease-and-desist over a cloud-reconnecting OrcaSlicer fork**, not the firmware-signing wall itself.

## 3.1 The fork and the C&D (Verified)

Developer **Paweł Jarczak** built **`OrcaSlicer-bambulab`**, an OrcaSlicer fork that **re-attached Orca to Bambu's cloud** (login / MakerWorld / cloud print) **without** going through Bambu Connect. Bambu pressured him to remove it; Jarczak **took the repo down voluntarily**, stating:
> "I removed the repository voluntarily. That removal should not be interpreted as an admission that all legal or technical allegations made against the project were correct."
- Sources: [Tom's Hardware — project shuttered](https://www.tomshardware.com/3d-printing/developer-re-enables-3d-printer-features-that-bambu-lab-disabled-firm-promptly-threatens-legal-action-orcaslicer-bambulab-project-now-shuttered), [Consumer Rights Wiki — C&D](https://consumerrights.wiki/w/Bambu_Lab_cease_and_desist_against_OrcaSlicer_fork_developer).

## 3.2 Bambu's position (Verified)

Bambu's blog **"Setting the record straight on Cloud Access and Community"** (**May 7, 2026**) argued the fork crossed from legitimate AGPL modification into impersonation (notably **without naming Jarczak**):
> "The modification in question worked by injecting falsified identity metadata into network communication."
> "It pretended to be the official Bambu Studio client when communicating with our servers."
> "Modifying and distributing AGPL code—absolutely. But impersonating official clients in communication with cloud infrastructure is not allowed."
- Source: [Bambu blog — Setting the record straight](https://blog.bambulab.com/setting-the-record-straight-on-cloud-access-and-community/).

## 3.3 Gamers Nexus + Rossmann respond (Verified)

Gamers Nexus published **"Fuck You, Bambu Lab: OrcaSlicer-BambuLab Download (with permission)"** by **Steve Burke** on **May 12, 2026**. GN relays Bambu's phrases ("falsified identity metadata," "pretended to be the official Bambu Studio client," "crosses into impersonation," "bypassing a technical limitation") and takes the developer's side:
> "Pawel took down the software out of an abundance of caution … we believe Pawel is in the right to both make and upload his software."

**Gamers Nexus and Louis Rossmann each pledged $10,000 toward Jarczak's legal defense**, GN said it **will host the software**, and invited Bambu to *"add us to their list of lawsuits."* Tom's Hardware separately reported Rossmann **re-hosting the banned fork and daring the company to sue**, with **other creators pledging support, boycott calls, and Snapmaker donating equipment** to the developer.
- Sources: [Gamers Nexus — "Fuck You, Bambu Lab"](https://gamersnexus.net/fk-you-bambu-lab), [Tom's Hardware — Rossmann re-hosts](https://www.tomshardware.com/3d-printing/louis-rossmann-taunts-bambu-lab-by-hosting-banned-3d-printer-firmware-fork-dares-usd1-billion-company-to-sue-him-more-creators-pledge-support-and-boycotts-snapmaker-donates-equipment-to-embattled-developer).

> **Open item:** GN referenced a forthcoming companion deep-dive on its "GNCA Investigates" channel; the exact video title/URL/date was not available in this pass.

---

# Part 4 — Bambu Lab's Terms of Use (the contract layer)

## 4.1 The anti-reverse-engineering clause (Verified)

Bambu Lab's **Terms of Use § 3.4** ([bambulab.com/en-us/policies/terms](https://bambulab.com/en-us/policies/terms)) provides:
> "Except as otherwise expressly permitted, you shall not, nor allow any other person to misappropriate, intrude or make other inappropriate use of the Product, including, but not limited to modify, discoder [sic], copy, reverse engineer, publish, publicly disseminate, decompile, export codes, disassemble or create derivatives of the Product in any way."

Bambu has also publicly framed the limit on circumvention:
> "The AGPL, the DMCA, and Bambu Lab's terms do not permit reverse engineering that violates applicable protocols, rules, or circumvents technical protection measures protecting our cloud services."

- Source (clause text retrieved via search of the live ToU and Bambu's public statements): [Terms of Use | Bambu Lab US](https://bambulab.com/en-us/policies/terms).

## 4.2 The contract-vs-license contradiction (Verified analysis)

§3.4 forbids users from "modify[ing], copy[ing], reverse engineer[ing], or creat[ing] derivatives" of "the Product" — which **directly conflicts with the AGPL-3.0** under which Bambu Studio is distributed. **AGPL-3.0 §10** bars a licensor from imposing "further restrictions" on the rights the license grants. This is exactly the conflict the Software Freedom Conservancy raised (Part 5.4): a ToS clause that purports to ban what the AGPL affirmatively permits is, on SFC's view, itself a license violation.

> **Open items:** verbatim Bambu **EULA**, **Privacy Policy**, **Acceptable-Use**, and **Warranty** clauses (automated/programmatic access, telemetry, custom-firmware warranty voiding) were not individually quoted in this pass. The §3.4 reverse-engineering clause and Bambu's circumvention statement are the load-bearing ones for the comparison; the others should be quoted from the live policy pages (effective dates included) before any legal reliance.

---

# Part 5 — `beambam` vs. Bambu's terms — and the SFC AGPL finding

## 5.1 What `beambam` does (Repo-verified)

`beambam` (originally `x2d`) is *"a drop-in replacement for the Bambu Network Plugin + Bambu Cloud"* (`README.md:10`) for the printers/platforms Bambu's stack doesn't serve. Capabilities relevant here:

| Capability | How | File(s) |
|---|---|---|
| LAN-only (no cloud login) | MQTT 8883 + access code | `beambam/mqtt.py` |
| **Signed MQTT** | RSA-SHA256; leaked Bambu Connect cert *or* recovered per-install key | `beambam/mqtt_sign.py`, `bambu_cert.py` |
| **Print start over pure LAN** | FTPS upload + signed `print.project_file` with **`url_enc`** (RSA-encrypted file location to printer's device cert) | `beambam/ftps.py`, `beambam/lan_print.py` |
| **Per-install key recovery** | Reads the RSA key from Bambu Handy's **Dart heap** (root+adb; no Frida) | `runtime/handy_extract/extract_signing_key.py` |
| Device-cert fetch | Unsigned `security.app_cert_install` → printer returns its device cert | `beambam/device_cert.py` |
| Reversed cloud API | Endpoints inferred from pybambu/OrcaSlicer/bambu-node | `cloud_client.py` |
| **`libbambu_networking.so` ABI shim** | Lets the GUI drive printers via this bridge instead of Bambu's proprietary plugin | `runtime/network_shim/` (AGPL-scoped) |

Uniquely (per `README.md:83-92`, `docs/COMPARISON.md:39-50`), it **starts a print on authorization-control firmware over pure LAN with no cloud account and no Developer Mode** — the one combination every other open client either can't do or only does by dropping into Developer Mode.

## 5.2 Licensing (Repo-verified)

- This repo's code is **MIT** (`LICENSE:1-3`, "Copyright (c) 2025-2026 Will Stone"), covering the `beambam` package/bridge/daemon/MCP/HA/web UI/slicing helpers (`LICENSE:25-27`).
- **AGPL-3.0 carve-outs** (`LICENSE:29-39`): `BambuStudio/`, `bs-bionic/`, `bs-cli/`, `bs-gui/`, `patches/*.patch`, `runtime/network_shim/`, `runtime/bambu_extract/`.
- **The PyPI package does not link AGPL code at runtime** — it "talks to printers over the wire (signed MQTT, FTPS, HTTP)" (`LICENSE:41-43`), keeping the shipped Python MIT-clean.
- BambuStudio is **AGPL-3.0** (forked from PrusaSlicer→Slic3r), with the **Bambu Network Plugin carved out as proprietary/"non-free."** OrcaSlicer is **AGPL-3.0**.

## 5.3 Mapping `beambam` against Bambu ToS §3.4 (Analysis)

| `beambam` behavior (Repo-verified) | Bambu ToS §3.4 / cloud-circumvention stance | Tension |
|---|---|---|
| Recover per-install RSA key from Handy's heap | "reverse engineer … decompile … disassemble"; circumvents an authentication measure | **High** (DMCA §1201(a) also implicated) |
| Sign `print.*` to satisfy authorization control | circumvents a technical protection measure | **High** |
| Ship the leaked Bambu Connect cert (`bambu_cert.py`) | circumvents a TPM | **High** |
| Reverse-engineer cloud endpoints (`cloud_client.py`) | "reverse engineer"; "circumvents TPMs protecting our cloud services" | **Med–High** (cloud only) |
| `libbambu_networking.so` ABI shim | "create derivatives"; but **this is the very lib SFC says Bambu unlawfully withholds source for** (5.4) | **Contested both ways** |
| LAN-only control of your own printer (no cloud) | mostly outside the cloud-circumvention stance | **Low** — strongest footing |

**The asymmetry that matters:** `beambam`'s **LAN-only / no-cloud** paths are its strongest position (own device, own network — Part 1.6/1.7, *Van Buren*). The behaviors that touch **Bambu's cloud** are where Bambu's contract terms and residual CFAA risk reach. The **key-recovery + signing** path is where DMCA §1201 is most squarely implicated — and `beambam` does it for **print start**, the exact capability the firmware was built to gate.

## 5.4 The Software Freedom Conservancy's AGPL finding — and why it flips the script (Verified)

On **May 18, 2026** the **Software Freedom Conservancy** published **"Comprehensive Response to Bambu's AGPLv3 Violations,"** finding **two** violations:
1. **Missing Corresponding Source Code.** Bambu fails to provide complete source for its slicer's proprietary networking libraries:
   > "Bambu's failure to provide CCS and Installation Information for the libraries known as `libbambu_networking.so`, `bambu_networking.dll`, and `libbambu_networking.dylib` constitutes an egregious and ongoing violation of AGPLv3."
2. **Imposing "further restrictions."** The C&D against Jarczak's fork violated **AGPLv3 §10(3)**, which prohibits imposing *"further restrictions on the exercise of the rights granted or affirmed."*

SFC launched the funded **`baltobu`** project — **"reverse-engineering the proprietary networking libraries, maintaining an OrcaSlicer fork for Bambu compatibility, and developing a replacement Bambu Studio implementation"** (fundraising goal ~$250,007) — and announced a **monthly standing committee** (details June 2026) on 3D-printer software freedom. **Bambu backtracked**: per Notebookcheck (**May 23, 2026**), the company abandoned the C&D threat, saying:
> "We nonetheless regret that our reference to terms of service, legal context, and a potential C&D understandably came across as a legal threat. That was not the outcome we wanted."

Separately, **Josef Prusa** publicly argued Bambu's un-auditable networking "black box" is itself an AGPL problem and a security risk (Tom's Hardware).

- Sources: [SFC — Comprehensive Response](https://sfconservancy.org/news/2026/may/18/bambu-studio-3d-printer-agpl-violation-response/), [Notebookcheck — Bambu backtracks](https://www.notebookcheck.net/Bambu-Lab-backtracks-after-SFC-accuses-company-of-AGPL-violations-and-legal-threats.1303904.0.html), [Tom's Hardware — SFC steps in](https://www.tomshardware.com/3d-printing/open-source-non-profit-claims-bambu-lab-violated-license-move-follows-cease-and-desist-demand-on-orcaslicer-fork-that-restored-cloud-printing-features-without-using-bambu-connect), [Tom's Hardware — Prusa warning](https://www.tomshardware.com/3d-printing/josef-prusa-warns-chinese-3d-printing-software-poses-massive-security-risks-bambu-lab-allegedly-violates-agpl-license-with-an-un-auditable-network-black-box).

**Why this matters for `beambam`:** the lib SFC says Bambu **illegally withholds source for** — `libbambu_networking.so` — is the **same lib `beambam` reimplements** as an ABI shim (`runtime/network_shim/`). SFC's `baltobu` (reverse-engineer the networking libs, maintain an Orca fork, build a replacement Studio) is **functionally the same program `beambam` already is.** That reframes `beambam`'s reverse engineering: not as a rogue circumvention, but as the kind of interoperability/compliance work a major software-freedom nonprofit has now publicly funded — and it strengthens the §1201(f) interoperability and AGPL-§10 arguments (Bambu can't both ship AGPL code and contractually forbid the reverse engineering needed to interoperate with the source it's withholding).

---

# Part 6 — Synthesis: where ownership rights and the ToS collide

1. **The fight is structural.** Authorization control converts a *policy* preference (use our cloud/app) into a *cryptographic* fact (the printer won't obey unsigned commands). That's the hardware/firmware-license + TPM combination of Part 1 — which is why the only sanctioned local path (Developer Mode) costs you the cloud.
2. **The "security" framing is weak on the record.** The Bambu Connect private key was extracted in three days (2.3); SFC and Prusa argue the networking layer is an un-auditable AGPL "black box" (5.4). The lock reads, on the public record, more as control than as security.
3. **`beambam` sits exactly where the law is contested.** Cleanest: LAN-only read/control of your own printer; protocol RE for interoperability; the MIT package not linking AGPL. Riskiest: key-recovery + signing `print.*` (DMCA §1201(a) circumvention) and **distributing** that capability (anti-trafficking — no exemption, even the 2024 repair exemption only covers an owner's *own-device* circumvention, not tool distribution).
4. **Three fronts, one war.** (a) The **firmware-signing wall** (a §1201 / interoperability question `beambam` answers technically); (b) the **C&D / right-to-tinker fight** (GN + Rossmann + the fork); (c) the **AGPL-compliance fight** (SFC's CCS finding + `baltobu`). All reduce to the report's title question: *who controls the printer you paid for?* By mid-2026 the answer is shifting toward owners — Bambu backtracked, SFC is funding the open replacement, and creators are re-hosting in the open.
5. **Risk-reduction observations (not legal advice).** This repo ships **no usage disclaimer**. Its strongest framing is the one the law and SFC both support: interoperability + repair + owner self-control of one's own device on one's own LAN, by people who own the hardware. The genuine exposure concentrates in **distributing** the circumvention capability and in **cloud-touching** paths — the LAN-only core is well-defended.

---

## Open items (honest gaps)

1. **Bambu EULA / Privacy / Acceptable-Use / Warranty** verbatim clauses (beyond ToU §3.4) — quote from the live policy pages with effective dates before legal reliance.
2. **The exact "Developer Mode / LAN-only" walkback** wording/timeline Bambu issued after the Jan 2025 backlash (treated here via the repo + ecosystem, not a single pinned primary source).
3. **GN's companion "GNCA Investigates" video** — title/URL/date not captured.
4. **Application of the §1201 anti-trafficking provisions to a distributed tool like `beambam`** is genuinely unlitigated; the 2024 repair exemption covers owner self-repair, not distribution. *Van Buren* (CFAA) and *Google v. Oracle* (fair-use interoperability) are favorable but none was litigated against facts identical to `beambam`.

## Appendix — locally verified repo sources
`README.md`; `LICENSE` (1–43); `docs/COMPARISON.md` (firmware versions/dates/HMS; Developer-Mode mechanics; OpenBambuAPI cert scheme; the GN/Jarczak C&D framing); `runtime/handy_extract/` (`DART_HEAP_KEY_EXTRACTION.md`, `SIGNER_HANDOFF.md`, `extract_signing_key.py`); `runtime/network_shim/` (the `libbambu_networking.so` shim — the lib at the center of SFC's CCS finding); `docs/SIGNED_VS_UNSIGNED.md`, `docs/LOCAL_CONTROL_PATHS.md`; `cloud_client.py`, `bambu_cert.py`, `beambam/mqtt_sign.py`, `beambam/device_cert.py`; `BambuStudio/LICENSE` + `BambuStudio/README.md`.
