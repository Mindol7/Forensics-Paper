---
subject: "Paper title"
identifier: "{{Identifier in System}}"
classification:
  # Techniques (Level 3) this paper covers; keep the rows that apply, delete the rest
  # PREP
  - PREP-01-01  # Investigation Planning
  - PREP-01-02  # Securing Legal Authority
  # IDEC
  - IDEC-01-01  # On-scene Search and Digital Device Detection
  - IDEC-01-02  # Network Resource Identification
  - IDEC-02-01  # Physical Device Prioritization
  # PRCP
  - PRCP-01-01  # On-scene Device Access Control
  - PRCP-01-02  # Live System Isolation
  - PRCP-01-03  # Signal Isolation and Write Blocking
  - PRCP-01-04  # Preservation Request
  # COLL
  - COLL-01-01  # Forensic Data Triage
  - COLL-01-02  # Physical Data Acquisition
  - COLL-01-03  # Logical Data Acquisition
  - COLL-02-01  # Network Packet and Log Collection
  - COLL-02-02  # Open Source Intelligence (OSINT) Collection
  - COLL-02-03  # Anonymous Network and Dark Web Data Collection
  - COLL-02-04  # Cloud Data Collection
  - COLL-03-01  # Forensic Image Format
  # PSCP
  - PSCP-01-01  # Evidence Integrity Verification
  - PSCP-01-02  # Physical Storage Environment and Media Management
  - PSCP-01-03  # Retention Period and Legal Requirement Management
  - PSCP-01-04  # Disposal Management
  # EXAM
  - EXAM-01-01  # Data Normalization and Indexing
  - EXAM-01-02  # Data Filtering and Reduction
  - EXAM-02-01  # Virtualization System Examination
  - EXAM-02-02  # Container System Examination
  - EXAM-03-01  # Partition and Volume Examination
  - EXAM-03-02  # File System Examination
  - EXAM-03-03  # Operating System Artifact Examination
  - EXAM-03-04  # OS-level Memory Examination
  - EXAM-04-01  # Application Artifact Examination
  - EXAM-04-02  # Application-level Memory Examination
  - EXAM-04-03  # File Storage Format Examination
  - EXAM-05-01  # Network Packet and Log Examination
  - EXAM-05-02  # OSINT Examination
  - EXAM-05-03  # Anonymous Network and Dark Web Data Examination
  - EXAM-05-04  # Cloud Data Examination
  # ANYS
  - ANYS-01-01  # Relevant Content Analysis
  - ANYS-01-02  # Integrated Timeline Analysis
  - ANYS-01-03  # Multi-dimensional Relationship Analysis
  - ANYS-01-04  # Event Reconstruction
  # REPT
  - REPT-01-01  # Forensic Information Visualization
  - REPT-01-02  # Forensic Report Writing
  - REPT-01-03  # Evidence Admissibility Review
  # CMNF
  - CMNF-01-01  # Damaged Device Handling
  - CMNF-01-02  # Cryptographic Decryption
  - CMNF-01-03  # Protection Mechanism Bypass
  - CMNF-01-04  # Anti-forensic Indicator Identification
  - CMNF-01-05  # Deleted/Damaged Data Recovery
  - CMNF-01-06  # Forgery and Tampering Detection
  - CMNF-01-07  # Concealment Detection
  - CMNF-01-08  # De-anonymization
  - CMNF-01-09  # Trusted Execution Environment (TEE) Handling
  - CMNF-02-01  # Reference Data Sets
  - CMNF-02-02  # Common Representation of Digital Evidence
  - CMNF-02-03  # Forensic Tool Validation
  - CMNF-02-04  # Forensic Readiness
  - CMNF-02-05  # Forensic Education and Competency Framework
  - CMNF-02-06  # Laboratory Establishment
  # CVGF
  - CVGF-01-01  # Event Monitoring, Logging and Analysis
  - CVGF-01-02  # Cyber Threat Intelligence (CTI) Utilization
  - CVGF-01-03  # Malware Response
  - CVGF-01-04  # Ransomware Response
  - CVGF-01-05  # Technology Leakage Response
  - CVGF-01-06  # Personal Data Breach Response
  - CVGF-01-07  # Copyright Infringement Response
  - CVGF-02-01  # Media Source Analysis
  - CVGF-02-02  # Media Tampering Detection and Integrity Verification
  - CVGF-02-03  # Multimodal Content Analysis
  - CVGF-03-01  # On-chain Transaction Tracing
  - CVGF-03-02  # On-chain Obfuscation Countermeasures
  - CVGF-03-03  # Off-chain Transaction Tracing
  - CVGF-04-01  # Mobility (Drone, Vehicle, Vessel) Forensics
  - CVGF-04-02  # Robot Forensics
  - CVGF-04-03  # Healthcare Forensics
  - CVGF-04-04  # Industrial Control System (ICS/SCADA) Forensics
  - CVGF-04-05  # Aerospace Forensics
  - CVGF-05-01  # Intelligent Forensic Analysis Automation
  - CVGF-05-02  # Forensics of AI Systems
  - CVGF-05-03  # AI-generated Content Identification and Abuse Response
tags:
  # Keep only the rows that apply; delete the rest
  # DEV
  - DEV-PC            # PC
  - DEV-MOB           # Mobile
  # FS
  - FS-BLK            # Block-device-based
  - FS-FLH            # Flash-memory-based
  - FS-NET            # Network
  - FS-DST            # Distributed
  - FS-OPT            # Optical media
  - FS-ETC            # Special-purpose / other
  # OS
  - OS-WIN            # Windows
  - OS-LNX            # Linux
  - OS-MAC            # macOS
  - OS-AND            # Android
  - OS-IOS            # iOS
  - OS-ETC            # Special-purpose / other
  # APP
  - APP-WEB           # Web browser
  - APP-MES           # Messenger
  - APP-COL           # Collaboration
  - APP-SNS           # SNS
  - APP-EML           # Email
  - APP-GRW           # Groupware
  - APP-ERP           # ERP
  - APP-CNT           # Electronic contract & signature
  - APP-ACC           # Accounting
  - APP-CST           # Cloud storage
  - APP-SHA           # File transfer / sharing
  - APP-ARC           # File compression / archiving
  - APP-REC           # File backup / recovery
  - APP-DOC           # Document viewing / editing
  - APP-CCP           # Calendar, contacts & PIM
  - APP-PRJ           # Project & task management
  - APP-MUL           # Multimedia playback / editing
  - APP-GAM           # Games
  - APP-CWA           # Crypto wallet
  - APP-PWD           # Password management & auth
  - APP-RMT           # Remote access & control
  - APP-VPN           # VPN, proxy & anonymization
  - APP-SEC           # Security & endpoint management
  - APP-DEV           # Software development
  - APP-GAI           # Generative AI & AI agents
  - APP-WSV           # Web server
  - APP-DSV           # Database server
  - APP-ETC           # Special-purpose / other
  # CLOUD
  - CLOUD-SaaS:MES    # Messenger
  - CLOUD-SaaS:COL    # Collaboration
  - CLOUD-SaaS:SNS    # SNS
  - CLOUD-SaaS:EML    # Email
  - CLOUD-SaaS:GRW    # Groupware
  - CLOUD-SaaS:CST    # Storage
  - CLOUD-SaaS:ERP    # ERP
  - CLOUD-SaaS:CNT    # Electronic contract & signature
  - CLOUD-SaaS:ACC    # Accounting
  - CLOUD-SaaS:PRJ    # Project & task management
  - CLOUD-SaaS:GAI    # Generative AI
  - CLOUD-DaaS
  - CLOUD-IaaS
last_updated: "YYYY-MM-DDTHH:MM:SSZ"
author: "author"
---

# Paper title

## 1. Basic Information
| Item | Value |
|---|---|
| DOI | |
| URL | |
| Type | Article / Conference Paper / Review / Thesis / Book Chapter |
| Journal | |
| Vol. and Issue | |
| Publisher | |
| Publication Date | YYYY. MM. |
| OA | O / X |
| Authors | comma separated |
| Affiliations | separate with <br> |

## 2. Abstract
<div style="border:1px solid #ccc; padding:12px; border-radius:6px;">
Original abstract
</div>

## 3. Keywords
keyword1, keyword2, keyword3

## 4. Target Applications
<!-- registered target -> ID, otherwise blank -->
| ID | Name |
|---|---|
| <!-- Identifier of the level5 identifier within the system --> | <!-- Name of the Application --> |

## 5. References
<!-- registered work -> ID, otherwise blank. Web sources as [title](URL) -->
| ID | Title |
|---|---|
| <!-- Identifier of the reference within the system --> | <!-- Title of the reference --> |
