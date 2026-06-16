# Who Controls Your Printer? Consumer Device Rights, the Bambu Lab Backlash, the Gamers Nexus / SFC Fight, and Where `beambam` Stands

*An analysis of device-ownership rights, the 2025 Bambu Lab "authorization control" firmware controversy, the 2026 Gamers Nexus / Louis Rossmann / Software Freedom Conservancy legal flashpoint, and how this repository (`beambam`/`x2d`) maps against Bambu Lab's Terms of Use and the AGPL.*

Compiled 2026-06-15.

---

## About this report

External legal, regulatory, and news claims are cited inline to primary or authoritative sources (statutes, court opinions, the Federal Register, official legislature texts, company policy pages and blog posts, and reputable reporting). Claims about this repository cite file paths and line numbers. Bambu Lab's Terms of Use are quoted verbatim from the live page (last updated 24 April 2024). A short list of remaining open questions is at the end.

---

## Executive summary

1. **You own the hardware; the manufacturer treats the software as licensed.** That split — plus DMCA §1201 anti-circumvention — is the lever locked-down device vendors pull. US and EU law has been moving *toward* owners (Right-to-Repair, a 2024 DMCA repair exemption, *Van Buren*, *Google v. Oracle*), but with sharp limits exactly where firmware locks live.
2. **On January 16, 2025 Bambu Lab shipped "authorization control" firmware** that cryptographically gates printer connection and control — including *starting a print*, even in LAN mode — and **prohibits unauthorized third-party software** from critical operations, routing third-party tools through a new "Bambu Connect" client.
3. **The "security" rationale was undercut within days:** the Bambu Connect app's X.509 certificate and private key were extracted on January 19, 2025 because they shipped in plaintext inside an Electron app.
4. **The drama escalated dramatically in May 2026.** Bambu pressured developer Paweł Jarczak over his `OrcaSlicer-bambulab` fork (which re-attached Orca to Bambu's cloud), invoking "impersonation" / "falsified identity metadata"; Jarczak pulled the repo; **Gamers Nexus and Louis Rossmann re-hosted it, each pledged $10,000 for his legal defense, and dared Bambu to sue.**
5. **The Software Freedom Conservancy then found Bambu in violation of the AGPLv3** — for withholding the corresponding source of its proprietary networking libraries (`libbambu_networking.so`/`.dll`/`.dylib`) and for imposing "further restrictions" via the C&D — and launched the funded **`baltobu`** project to reverse-engineer those libraries and maintain open forks. **Bambu backtracked.**
6. **`beambam` (this repo) is a parallel of exactly what SFC's `baltobu` set out to do:** an AGPL-3.0 pure-LAN client that reimplements Bambu's networking/control plane. It uniquely starts a print on authorization-control firmware over pure LAN with **no cloud and no Developer Mode**, by recovering a per-installation signing key from a Bambu Handy install and RSA-encrypting the file location to the printer's device certificate.
7. **The law is genuinely split on the riskiest pieces.** Reverse-engineering-for-interoperability and LAN-only self-control of your own device are strongly protected (*Sega*, *Connectix*, *Google v. Oracle*, §1201(f), *Van Buren*). But **circumventing the signing TPM** sits in §1201's gray zone, and **distributing a circumvention tool** is the single highest-risk act — protected by *no* triennial exemption. Bambu's Terms of Use §3.4 also purports to forbid reverse engineering outright — a clause the SFC says is itself an unlawful "further restriction" on AGPL rights.

---

# Part 1 — Consumer rights to use, repair, and control devices you own

## 1.1 Ownership vs. licensing

You own the **physical object** under the **First Sale Doctrine**, *17 U.S.C. §109(a)* — you may use, resell, or dispose of that copy. Manufacturers structure the **firmware** as a *license* so you never acquire the rights to copy/modify it; under ***Vernor v. Autodesk***, 621 F.3d 1102 (9th Cir. 2010), a user is a *licensee* (not owner) when the vendor labels it a license, restricts transfer, and imposes use restrictions — which, if met, defeats the *17 U.S.C. §117* software-owner self-help rights. *Vernor* binds only the 9th Circuit; the doctrine is **unsettled** elsewhere.
- Sources: [17 U.S.C. §109](https://www.law.cornell.edu/uscode/text/17/109), [17 U.S.C. §117](https://www.law.cornell.edu/uscode/text/17/117).

## 1.2 DMCA §1201 anti-circumvention + the 2024 repair exemption

*17 U.S.C. §1201* bans (a)(1)(A) the **act** of circumventing an **access**-control TPM; and (a)(2)/(b)(1) **trafficking** in circumvention **tools**. It also carries permanent exceptions including **§1201(f) reverse engineering for interoperability** and **§1201(j) security testing**.
- **The triennial process — §1201(a)(1)(B)–(D):** every three years the **Librarian of Congress, on the Register of Copyrights' recommendation,** adopts temporary exemptions to the *act* ban.
- **The Ninth Triennial final rule** (FR Doc. **2024-24563**, **89 FR 85437**) became **effective October 28, 2024** and runs to **October 28, 2027**. It grants an exemption at **37 CFR 201.40(b)(15)** for:
  > "Computer programs that are contained in and control the functioning of a lawfully acquired device that is primarily designed for use by consumers, when circumvention is a necessary step to allow the diagnosis, maintenance, or repair of such a device, and is not accomplished for the purpose of gaining access to other copyrighted works."
- **Critical limits:** the exemption covers an **owner circumventing for repair/maintenance/diagnosis of their own device** — it does **not** cover modification beyond repair, and it does **not** cover **distributing** circumvention tools (the anti-trafficking provisions §1201(a)(2)/(b) are untouched, and no exemption ever reaches them). No exemption squarely names "3D-printer firmware."
- Sources: [Federal Register — final rule 2024-24563](https://www.federalregister.gov/documents/2024/10/28/2024-24563/exemption-to-prohibition-on-circumvention-of-copyright-protection-systems-for-access-control), [17 U.S.C. §1201 (Cornell LII)](https://www.law.cornell.edu/uscode/text/17/1201), [Copyright Office 1201/2024](https://www.copyright.gov/1201/2024/).

## 1.3 Right-to-Repair laws

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

## 1.4 Magnuson-Moss Warranty Act — anti-tying

The FTC has **enforced** the Magnuson-Moss anti-tying rule: in 2022 it acted against **Weber-Stephen**, **MWE Investments (Westinghouse)**, and **Harley-Davidson** for voiding warranties when customers used third-party parts/independent repair — *"void if customers used independent repairers or third-party parts"* — with final orders approved **October 2022** (announced June–July 2022). Conditioning warranty coverage on brand-name parts/service violates the MMWA's anti-tying provision (*15 U.S.C. §2302(c)*). A maker cannot lawfully void a warranty merely because an owner ran third-party parts/software.
- Sources: [PIRG](https://pirg.org/edfund/resources/the-state-of-right-to-repair/), [FTC](https://www.ftc.gov/business-guidance/blog/2019/03/nixing-fix-warranties-mag-moss-restrictions-repairs).

## 1.5 Reverse engineering for interoperability

- ***Google LLC v. Oracle America***, decided **April 5, 2021 (6-2)**: copying the Java API **declaring code** for interoperability is **fair use**. The Court *assumed without deciding* copyrightability and resolved on fair use, treating declaring code's functional nature and role as a learned standard interface — "further than … most computer programs … from the core of copyright" — as favoring fair use.
  - Sources: [CRS LSB10597](https://www.congress.gov/crs-product/LSB10597), [SCOTUS opinion 18-956 (PDF)](https://www.supremecourt.gov/opinions/20pdf/18-956_d18f.pdf).
- ***Sega Enterprises v. Accolade***, 977 F.2d 1510 (9th Cir. 1992) and ***Sony Computer Entertainment v. Connectix***, 203 F.3d 596 (9th Cir. 2000): intermediate copying to reverse-engineer for interoperability is **fair use** — the bedrock interoperability line consistent with *Google v. Oracle*.
- **DMCA §1201(f)** codifies a reverse-engineering-for-interoperability exception for someone who lawfully obtained the program.

**Conditions to stay protected:** lawfully own the device/software; copy only what's needed to reach functional/interface elements; **don't redistribute the firmware**; build something independently created. **Overlay risk:** anti-RE clauses in a EULA/ToS may bind by *contract* even where copyright would permit (enforceability contested — and see SFC's AGPL §10 argument in Part 5.4).

## 1.6 CFAA after *Van Buren*

***Van Buren v. United States***, No. 19-783, decided **June 3, 2021 (6-3)**: a person "exceeds authorized access" only by obtaining information in **areas of a computer that are off-limits** to them — a **"gates-up-or-down"** inquiry keyed to *whether* access is permitted, **not** the *purpose*. The Court held Congress's removal of the 1984 Act's "purpose" reference "cuts against" a purpose-based reading.
> "An individual exceeds authorized access when he accesses a computer with authorization but then obtains information located in particular areas of the computer—such as files, folders, or databases—that are off-limits to him."
- **Implication:** controlling **your own LAN device** is the safest posture — you are the authorized party, there is no gate to breach. **Merely violating a ToS is unlikely a CFAA crime** post-*Van Buren* — though a ToS can still bind you by **contract** (cf. *hiQ v. LinkedIn*, where scraping public data wasn't CFAA but the defendant still lost on breach of contract). Risk concentrates when you touch the **vendor's cloud**.
- Sources: [SCOTUS opinion 19-783 (PDF)](https://www.supremecourt.gov/opinions/20pdf/19-783_k53l.pdf), [Cornell LII 19-783](https://www.law.cornell.edu/supremecourt/text/19-783), [CRS LSB10616](https://www.congress.gov/crs-product/LSB10616).

## 1.7 Synthesis for 3D printers

| Owner action | Footing | Strength |
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

## 2.1 What shipped

On **January 16, 2025** Bambu Lab announced a firmware-level *"authorization and authentication protection mechanism for the connection and control of Bambu Lab 3D printers"* (post updated Jan 17 & 20). Beta firmware **01.08.03.00** shipped **Jan 17, 2025**; full release followed in **late January** (this repo's `docs/COMPARISON.md` records the full build as `01.08.05.00`). Gated operations include:
> "Initiating a print job (via LAN or cloud mode). Controlling motion system, temperature, fans, AMS settings, calibrations, etc." — plus binding/unbinding, remote video, and firmware upgrades.

And critically:
> "Unauthorized third-party software will be prohibited from executing critical operations." … third-party slicers "will no longer be able to utilize Studio network plugin API for authorization control."

**Authorization applies even in LAN mode**, on Bambu's stated rationale:
> "even when the printer is in LAN mode, the network environment in which the printer is located may still be connected to the public network, and other malicious software may still be able to remotely access the printer."

- Sources: [Bambu blog — authorization control](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/), [Hackaday 2025-01-17](https://hackaday.com/2025/01/17/new-bambu-lab-firmware-update-adds-mandatory-authorization-control-system/), [3D Printing Industry](https://3dprintingindustry.com/news/bambu-lab-controversy-deepens-firmware-update-sparks-backlash-240588/), [Consumer Rights Wiki — Authorization Control System](https://consumerrights.wiki/w/Bambu_Lab_Authorization_Control_System), [HN discussion](https://news.ycombinator.com/item?id=42764277).

**Technical detail** (`docs/COMPARISON.md`): the printer verifies every MQTT control command and rejects anything unsigned or signed by an unrecognized cert (`MQTT command verification failed`, HMS `0500-0500-0001-0007`). Per `Doridian/OpenBambuAPI` it requires an **RSA-SHA256 envelope signed by a per-device cert chaining to the Bambu CA with a CN matching the printer's serial** — so a single shared/leaked cert cannot satisfy `print.*`.

## 2.2 "Bambu Connect," the sanctioned replacement

Bambu introduced **Bambu Connect** as the official replacement for the Studio network-plugin API: it *"securely transmits sliced Bambu Lab G-code and 3MF files to your printer"* — a transmission/control conduit, not a slicer. Source: [Bambu blog](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/), [Bambu wiki — Bambu Connect](https://wiki.bambulab.com/en/software/bambu-connect).

## 2.3 The "security" rationale collapses in 3 days

On **January 19, 2025**, Hackaday reported **"Bambu Connect's Authentication X.509 Certificate And Private Key Extracted"** — a user **[hWuxH]** de-obfuscated the Electron app's `main.js`, found the cert and **private key in plaintext**, and posted it publicly.
> "These are used to encrypt HTTP traffic with the printer, and is the sole thing standing in the way of tools like OrcaSlicer talking with authentication-enabled Bambu Lab printers."

The reporting concluded *"security through obfuscation is not going to be very effective here."* A single, publicly distributed private key being the basis of an advertised "security" feature is the core community grievance.
- Source: [Hackaday 2025-01-19](https://hackaday.com/2025/01/19/bambu-connects-authentication-x-509-certificate-and-private-key-extracted/).

## 2.4 Custom firmware, LAN-only, and backlash

- **X1Plus** — an open-source custom firmware for Bambu printers; it tells the auto-update mechanism the device runs version *"99 or higher"* to prevent official firmware from overwriting it. Installation **voids warranty**. (Consumer Rights Wiki.)
- **Full local access** can be retained by running LAN-only **without cloud** — but on the new firmware that practically means **staying on / reverting to older firmware**. (Consumer Rights Wiki.)
- **Backlash:** customer reaction on forums and Reddit was "negative"; Bambu's Trustpilot page recorded *"a wave of one-star reviews"* citing the restrictions.
- Source: [Consumer Rights Wiki — Authorization Control System](https://consumerrights.wiki/w/Bambu_Lab_Authorization_Control_System).

## 2.5 The walkback — "Developer Mode"

On **January 20, 2025** Bambu responded to the backlash (3D Printing Industry, "Bambu Lab Responds to Backlash Over New Firmware Update"), characterizing the reaction as "a mix of valuable feedback and unfortunate misinformation circulating online" and calling claims about "remote printer disabling, firmware blocking prints, AMS restrictions, filament blocking, trojans, killswitches, file monitoring, and subscription requirements" **"entirely false."**

The concrete concession was an **optional LAN "Developer Mode"** (added after feedback from print farms) that keeps **"MQTT channels, video live streams, and File Transfer Protocol (FTP)"** open — but the user must enable it manually and "assume full responsibility for securing their local network," with no official support. Bambu also said users who stay on older firmware "will still be able to use both old and future versions of the Bambu Studio slicer and Bambu Handy application," and that Bambu Connect's LAN mode "does not require internet access or a user account."

The ecosystem trade-off (`docs/COMPARISON.md:16-23`): **Developer LAN Mode by design severs Bambu Cloud and disables auth verification** — so the sanctioned way to regain full local/third-party control costs you cloud, MakerWorld sync, and remote access. That is precisely the trade-off `beambam` avoids (Part 5.1).
- Sources: [3D Printing Industry — Bambu responds](https://3dprintingindustry.com/news/bambu-lab-responds-to-backlash-over-new-firmware-update-235771/), [All3DP — promises Developer Mode](https://all3dp.com/4/bambu-lab-responds-to-security-update-controversy-promises-developer-mode/).

**Causation — what was, and was not, a response to the backlash:**
- **Plain "LAN mode" was *not* added in response to anything** — it pre-dated the controversy; the original grievance was that authorization control applied *even in LAN mode*. Bambu confirmed ordinary LAN mode stays (community: "LAN mode isn't going away"; "Unsupported Developer Mode = what was sold to us as supported 'LAN Mode' when we bought the printer").
- **"Developer Mode" *was* a concession to the outcry.** Per 3D Printing Industry reporting Bambu's communications, it was added "following feedback from 3D print farms"; the contemporaneous community thread (started Jan 20 2025) reads: "After tremendous outcry from users and influencers over the new firmware 'Authorization' fiasco, Bambu Lab has made some small concessions and will include a 'Developer Mode' … likely would not have been included had we not spoken out strongly."
- **The reverse-engineering work was an *indirect* driver, not the stated cause.** No source shows Bambu attributing Developer Mode to the RE/key-extraction; Bambu credited "feedback" (and dismissed much criticism as "misinformation"). But the RE-adjacent grievances fed the backlash — third-party software (OrcaSlicer/Home Assistant) breakage is the *interoperability* problem RE solves, and the **Jan 19 Bambu Connect private-key extraction** publicly discredited the "security" rationale the day before the Jan 20 concession. The community thread itself demands documented APIs "so this should not need to be reverse engineered by users" — framing RE as the symptom, not the cited cause.
- Source: [Bambu forum — "Developer Mode - Help Define!" (Jan 20 2025)](https://forum.bambulab.com/t/developer-mode-help-define/137639).

---

# Part 3 — The Gamers Nexus / Louis Rossmann flashpoint (May 2026)

> The flashpoint is **a cease-and-desist over a cloud-reconnecting OrcaSlicer fork**, not the firmware-signing wall itself.

## 3.1 The fork and the C&D

Developer **Paweł Jarczak** built **`OrcaSlicer-bambulab`**, an OrcaSlicer fork that **re-attached Orca to Bambu's cloud** (login / MakerWorld / cloud print) **without** going through Bambu Connect. Bambu pressured him to remove it; Jarczak **took the repo down voluntarily**, stating:
> "I removed the repository voluntarily. That removal should not be interpreted as an admission that all legal or technical allegations made against the project were correct."
- Sources: [Tom's Hardware — project shuttered](https://www.tomshardware.com/3d-printing/developer-re-enables-3d-printer-features-that-bambu-lab-disabled-firm-promptly-threatens-legal-action-orcaslicer-bambulab-project-now-shuttered), [Consumer Rights Wiki — C&D](https://consumerrights.wiki/w/Bambu_Lab_cease_and_desist_against_OrcaSlicer_fork_developer).

## 3.2 Bambu's position

Bambu's blog **"Setting the record straight on Cloud Access and Community"** (**May 7, 2026**) argued the fork crossed from legitimate AGPL modification into impersonation (notably **without naming Jarczak**):
> "The modification in question worked by injecting falsified identity metadata into network communication."
> "It pretended to be the official Bambu Studio client when communicating with our servers."
> "Modifying and distributing AGPL code—absolutely. But impersonating official clients in communication with cloud infrastructure is not allowed."
- Source: [Bambu blog — Setting the record straight](https://blog.bambulab.com/setting-the-record-straight-on-cloud-access-and-community/).

## 3.3 Gamers Nexus + Rossmann respond

Gamers Nexus published **"Fuck You, Bambu Lab: OrcaSlicer-BambuLab Download (with permission)"** by **Steve Burke** on **May 12, 2026**. GN relays Bambu's phrases ("falsified identity metadata," "pretended to be the official Bambu Studio client," "crosses into impersonation," "bypassing a technical limitation") and takes the developer's side:
> "Pawel took down the software out of an abundance of caution … we believe Pawel is in the right to both make and upload his software."

**Gamers Nexus and Louis Rossmann each pledged $10,000 toward Jarczak's legal defense**, GN said it **will host the software**, and invited Bambu to *"add us to their list of lawsuits."* Tom's Hardware separately reported Rossmann **re-hosting the banned fork and daring the company to sue**, with **other creators pledging support, boycott calls, and Snapmaker donating equipment** to the developer. GN also announced it is **"working on a full standalone piece for GNCA that will publish as a deep dive into Bambu's anti-consumer actions."**
- Sources: [Gamers Nexus — "Fuck You, Bambu Lab"](https://gamersnexus.net/fk-you-bambu-lab), [Tom's Hardware — Rossmann re-hosts](https://www.tomshardware.com/3d-printing/louis-rossmann-taunts-bambu-lab-by-hosting-banned-3d-printer-firmware-fork-dares-usd1-billion-company-to-sue-him-more-creators-pledge-support-and-boycotts-snapmaker-donates-equipment-to-embattled-developer), [Notebookcheck — GN and Rossmann take on Bambu](https://www.notebookcheck.net/Gamers-Nexus-and-Louis-Rossmann-take-on-Bambu-Lab-over-OrcaSlicer-fork-developer.1298428.0.html), [All3DP — C&D threat backfires](https://all3dp.com/6/bambu-labs-cd-threat-backfires-slicer-now-hosted-by-louis-rossmann-and-gamersnexus/).

---

# Part 4 — Bambu Lab's Terms of Use (the contract layer)

The full **Bambu Lab Terms of Use** ([bambulab.com/en-us/policies/terms](https://bambulab.com/en-us/policies/terms)) are quoted below verbatim. **Last updated: 24 April 2024.** The ToU define "the Product" as the device **plus its embedded software** (§1.1) and bind all of the following.

## 4.1 The licensing posture — you license the software, on one device

> **§2 End User Software Licence** — "You are granted a limited and non-exclusive licence to access and use this software … on only one Bambu Lab device … The software cannot be sold, transferred, or used for any other commercial purposes."
> **§4 Reservation of Rights** — "Bambu Lab and its licensors own and retain all rights and titles to the visual interfaces, graphics, design, firmware, software, services, and all other elements of the Product…"

This is the textbook *Vernor*-style license framing from Part 1.1: the buyer is positioned as a single-device **licensee** of the firmware, not its owner. §3.3 permits exactly **one** "one-off permanent transfer of the entire licence … along with the transfer of ownership of your Bambu Lab device."

## 4.2 The clauses `beambam` is in tension with

- **§3.1 (no third-party software using Bambu IP):** "You may not use Bambu Lab technology or Bambu Lab intellectual property to develop software or design, develop, manufacture, sell, or licence third-party devices/accessories associated with Bambu Lab Product without Bambu Lab's prior consent."
- **§3.4 (anti-reverse-engineering):** "Except as otherwise expressly permitted, you shall not, nor allow any other person to … modify, discoder [sic], copy, reverse engineer, publish, publicly disseminate, decompile, export codes, disassemble or create derivatives of the Product in any way."
- **§3.5(2) (no redistribution of software/code):** you may not "provide to third parties, or allow third parties to use the whole or part of the Software without obtaining Bambu Lab's written consent (including but not limited to apps, services, code, and source code)."
- **§3.5(3) (no deceptive use):** you may not "use the Product in a deceptive way or for deceptive purposes." — *this is the textual hook behind the "impersonation" theory used against the Jarczak fork (Part 3.2).*
- **§3.5(5) (anti-DRM/anti-bypass):** you may not "attempt to destroy, bypass, change, invalidate or escape from the Product and/or any digital rights management system that is part of the organic composition of the Product." — *this is the contractual analogue of the DMCA §1201 anti-circumvention claim against beambam's signing-bypass.*

## 4.3 Other notable clauses

- **§6 Consent to Use of Data (telemetry):** Bambu "may collect data from your device for analysis … device configuration data, app statistical data, and error log data," which "may be transferred to … countries outside of the country you reside" with possibly "different data protection laws, or such laws may not even exist." (The dedicated [Privacy Notice](https://bambulab.com/policies/privacy) governs account data.)
- **§7.2 (support window):** a "guaranteed five-year provision of software updates" and "a minimum of seven years of software security updates."
- **§7.4 (forced updates can gate printing):** "your product may block new print job before the updates is installed." — *the ToU expressly contemplate the printer refusing to print until firmware is updated.*
- **§9 (third-party G-code disclaimer):** Bambu "does not guarantee the availability of G-code generated by third-party software … and will not be responsible for product damage caused by running G-code generated by third-party software."
- **§11 (termination):** "BREACH OF YOUR OBLIGATIONS … MAY RESULT IN THE DEACTIVATION OF YOUR ACCOUNT AND LOSS OR RESTRICTION OF ACCESS TO THE CONTENT ASSOCIATED WITH THE PRODUCT."
- **§13.1 (modification = no liability):** "BAMBU LAB WILL NOT TAKE ANY RESPONSIBILITY FOR PRODUCT USE PROBLEMS CAUSED BY ABUSE, MISUSE, OR UNAUTHORISED MODIFICATION." (Note: under Magnuson-Moss §2302(c), Part 1.4, a *blanket* "modification voids everything" stance is unenforceable in the US absent proof the mod caused the specific defect.)
- **§13.3 (liability cap):** total liability shall not "EXCEED THE AMOUNT YOU PAID FOR YOUR BAMBU LAB PRODUCT."
- **§15 (export controls)** and **§17 (governing law):** disputes are "governed by the laws of the People's Republic of China," resolved by **CIETAC arbitration in Shanghai**, in English, before three arbitrators, "final and binding."

## 4.4 The contract-vs-license contradiction

§3.4 forbids "modify … reverse engineer … create derivatives of the Product," and §3.5(2) forbids redistributing "source code" — yet Bambu Studio is distributed under **AGPL-3.0**, whose **§10** bars a licensor from imposing "further restrictions" on the rights the license grants (and whose §1/§5 affirmatively grant modification, source access, and redistribution). A ToU that purports to ban what the AGPL affirmatively permits is, on the Software Freedom Conservancy's view (Part 5.4), **itself a license violation**. Note too the **jurisdictional tension**: §17 routes all disputes to PRC-law CIETAC arbitration, while the consumer-rights doctrines in Part 1 (DMCA exemptions, state R2R, Magnuson-Moss, *Van Buren*) are US law — a US owner's statutory rights are not waived by a choice-of-law clause (§13.4 concedes "Nothing in these Terms affects your legal rights that you are always entitled to as a consumer").

---

# Part 5 — `beambam` vs. Bambu's terms — and the SFC AGPL finding

## 5.1 What `beambam` does

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

## 5.2 Licensing

- **The whole project is AGPL-3.0-or-later** (`LICENSE`; Copyright © 2025–2026 Will Stone) — the `beambam` package/bridge/daemon/MCP/HA/web UI/slicing helpers, plus the bundled `BambuStudio/`, `bs-bionic/`, `bs-cli/`, `bs-gui/` fork, `patches/*.patch`, and `runtime/network_shim/` + `runtime/bambu_extract/`. (The Python tree was previously MIT; it was relicensed to AGPL-3.0 to bar closed-source forks — and, via AGPL §13, closed *hosted* forks.)
- The PyPI package is pure-Python and does **not** link the AGPL slicer at runtime — it talks to printers over the wire (signed MQTT, FTPS, HTTP) — but is itself AGPL-3.0.
- BambuStudio is **AGPL-3.0** (forked from PrusaSlicer→Slic3r), with the **Bambu Network Plugin carved out as proprietary/"non-free."** OrcaSlicer is **AGPL-3.0**.

## 5.3 Mapping `beambam` against the specific ToU clauses

| `beambam` behavior | Bambu ToU clause(s) implicated (Part 4) | Tension |
|---|---|---|
| Recover per-install RSA key from Handy's heap | §3.4 "reverse engineer … decompile … disassemble"; §3.5(5) "bypass … any digital rights management system" | **High** (DMCA §1201(a) also implicated) |
| Sign `print.*` to satisfy authorization control | §3.5(5) anti-bypass; circumvents a TPM | **High** |
| Ship the leaked Bambu Connect cert (`bambu_cert.py`) | §3.5(5) anti-bypass | **High** |
| Reverse-engineer cloud endpoints (`cloud_client.py`) | §3.4 "reverse engineer"; Bambu's "circumvents TPMs protecting our cloud services" stance | **Med–High** (cloud only) |
| Build third-party software for the printer | §3.1 "may not use Bambu Lab … IP to develop software … without … consent" | **Med–High** |
| `libbambu_networking.so` ABI shim | §3.4 "create derivatives"; but **this is the very lib SFC says Bambu unlawfully withholds source for** (5.4) — AGPL §10 may void the §3.4 restriction | **Contested both ways** |
| Distributing `beambam` (PyPI/GitHub) | §3.5(2) no providing "code, and source code" to third parties | **Med** (but the AGPL package doesn't redistribute Bambu's Software) |
| LAN-only control of your own printer (no cloud) | mostly outside the cloud-circumvention stance | **Low** — strongest footing |

**The asymmetry that matters:** `beambam`'s **LAN-only / no-cloud** paths are its strongest position (own device, own network — Part 1.6/1.7, *Van Buren*). The behaviors that touch **Bambu's cloud** are where Bambu's contract terms and residual CFAA risk reach. The **key-recovery + signing** path is where DMCA §1201 is most squarely implicated — and `beambam` does it for **print start**, the exact capability the firmware was built to gate.

## 5.4 The Software Freedom Conservancy's AGPL finding — and why it flips the script

On **May 18, 2026** the **Software Freedom Conservancy** published **"Comprehensive Response to Bambu's AGPLv3 Violations,"** finding **two** violations:
1. **Missing Corresponding Source Code.** Bambu fails to provide complete source for its slicer's proprietary networking libraries:
   > "Bambu's failure to provide CCS and Installation Information for the libraries known as `libbambu_networking.so`, `bambu_networking.dll`, and `libbambu_networking.dylib` constitutes an egregious and ongoing violation of AGPLv3."
2. **Imposing "further restrictions."** The C&D against Jarczak's fork violated **AGPLv3 §10(3)**, which prohibits imposing *"further restrictions on the exercise of the rights granted or affirmed."*

SFC launched the funded **`baltobu`** project — **"reverse-engineering the proprietary networking libraries, maintaining an OrcaSlicer fork for Bambu compatibility, and developing a replacement Bambu Studio implementation"** (fundraising goal ~$250,007) — and announced a **monthly standing committee** (details June 2026) on 3D-printer software freedom. **Bambu backtracked**: per Notebookcheck (**May 23, 2026**), the company abandoned the C&D threat, saying:
> "We nonetheless regret that our reference to terms of service, legal context, and a potential C&D understandably came across as a legal threat. That was not the outcome we wanted."

Separately, **Josef Prusa** publicly argued Bambu's un-auditable networking "black box" is itself an AGPL problem and a security risk (Tom's Hardware).

- Sources: [SFC — Comprehensive Response](https://sfconservancy.org/news/2026/may/18/bambu-studio-3d-printer-agpl-violation-response/), [Notebookcheck — Bambu backtracks](https://www.notebookcheck.net/Bambu-Lab-backtracks-after-SFC-accuses-company-of-AGPL-violations-and-legal-threats.1303904.0.html), [Tom's Hardware — SFC steps in](https://www.tomshardware.com/3d-printing/open-source-non-profit-claims-bambu-lab-violated-license-move-follows-cease-and-desist-demand-on-orcaslicer-fork-that-restored-cloud-printing-features-without-using-bambu-connect), [Tom's Hardware — Prusa warning](https://www.tomshardware.com/3d-printing/josef-prusa-warns-chinese-3d-printing-software-poses-massive-security-risks-bambu-lab-allegedly-violates-agpl-license-with-an-un-auditable-network-black-box).

**Why this matters for `beambam`:** the lib SFC says Bambu **withholds source for** — `libbambu_networking.so` — is the **same lib `beambam` reimplements** as an ABI shim (`runtime/network_shim/`). SFC's `baltobu` (reverse-engineer the networking libs, maintain an Orca fork, build a replacement Studio) is **functionally the same program `beambam` already is.** That reframes `beambam`'s reverse engineering: not as a rogue circumvention, but as the kind of interoperability/compliance work a major software-freedom nonprofit has now publicly funded — and it strengthens the §1201(f) interoperability and AGPL-§10 arguments (Bambu can't both ship AGPL code and contractually forbid the reverse engineering needed to interoperate with the source it's withholding).

---

# Part 6 — Synthesis: where ownership rights and the ToS collide

1. **The fight is structural.** Authorization control converts a *policy* preference (use our cloud/app) into a *cryptographic* fact (the printer won't obey unsigned commands). That's the hardware/firmware-license + TPM combination of Part 1 — which is why the only sanctioned local path (Developer Mode) costs you the cloud.
2. **The "security" framing is weak on the record.** The Bambu Connect private key was extracted in three days (2.3); SFC and Prusa argue the networking layer is an un-auditable AGPL "black box" (5.4). The lock reads, on the public record, more as control than as security.
3. **`beambam` sits exactly where the law is contested.** Cleanest: LAN-only read/control of your own printer; protocol RE for interoperability; the AGPL package not linking Bambu's proprietary code. Riskiest: key-recovery + signing `print.*` (DMCA §1201(a) circumvention) and **distributing** that capability (anti-trafficking — no exemption, even the 2024 repair exemption only covers an owner's *own-device* circumvention, not tool distribution).
4. **Three fronts, one war.** (a) The **firmware-signing wall** (a §1201 / interoperability question `beambam` answers technically); (b) the **C&D / right-to-tinker fight** (GN + Rossmann + the fork); (c) the **AGPL-compliance fight** (SFC's CCS finding + `baltobu`). All reduce to the report's title question: *who controls the printer you paid for?* By mid-2026 the answer is shifting toward owners — Bambu backtracked, SFC is funding the open replacement, and creators are re-hosting in the open.
5. **Risk-reduction observations (not legal advice).** This repo ships **no usage disclaimer**. Its strongest framing is the one the law and SFC both support: interoperability + repair + owner self-control of one's own device on one's own LAN, by people who own the hardware. The genuine exposure concentrates in **distributing** the circumvention capability and in **cloud-touching** paths — the LAN-only core is well-defended.

---

## Remaining open questions

1. **Standalone Bambu Privacy Notice & Warranty Statement** — the ToU's telemetry (§6) and modification/liability (§13.1) clauses are quoted; the dedicated `/policies/privacy` and `/policies/warranty` pages (account-data handling, RMA mechanics) are not quoted here.
2. **Application of the §1201 anti-trafficking provisions to a distributed tool like `beambam`** is genuinely unlitigated; the 2024 repair exemption covers owner self-repair, not distribution. *Van Buren* (CFAA) and *Google v. Oracle* (fair-use interoperability) are favorable, and SFC's `baltobu` is a strong precedent that this *kind* of work is legitimate, but none was litigated against facts identical to `beambam`.
3. **GN's GNCA deep-dive video** was announced as forthcoming; once published it will have its own title/URL/date to add.

## Appendix — repository sources
`README.md`; `LICENSE`; `docs/COMPARISON.md` (firmware versions/dates/HMS; Developer-Mode mechanics; OpenBambuAPI cert scheme; the GN/Jarczak C&D framing); `runtime/handy_extract/` (`DART_HEAP_KEY_EXTRACTION.md`, `SIGNER_HANDOFF.md`, `extract_signing_key.py`); `runtime/network_shim/` (the `libbambu_networking.so` shim — the lib at the center of SFC's CCS finding); `docs/SIGNED_VS_UNSIGNED.md`, `docs/LOCAL_CONTROL_PATHS.md`; `cloud_client.py`, `bambu_cert.py`, `beambam/mqtt_sign.py`, `beambam/device_cert.py`; `BambuStudio/LICENSE` + `BambuStudio/README.md`.
