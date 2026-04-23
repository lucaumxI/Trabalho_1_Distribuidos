# Documento de Requisitos: Sistema de Videoconferência Distribuído (ZeroMQ)

## 1. Visão Geral do Sistema
Ferramenta de videoconferência desktop que suporta transmissão de Vídeo, Áudio e Texto em ligações individuais e em grupo. O sistema evolui de um modelo centralizado para uma arquitetura distribuída, resiliente e escalável baseada em múltiplos brokers de mensagens.

## 2. Requisitos Funcionais (RF)
### Gestão de Identidade e Sessão
* **RF01:** O sistema deve permitir o login do usuário através de um ID único simples. (✅)
* **RF02:** O sistema deve controlar e manter o estado de presença dos usuários (quem está online). (✅)
* **RF03:** O sistema deve permitir a entrada e saída de usuários em salas/grupos de comunicação (ex: Grupos A–K). (✅)

### Transmissão e Interação
* **RF04:** O sistema deve capturar, enviar e receber Vídeo, Áudio e Texto. (✅)
* **RF05:** Os canais de transmissão para voz, vídeo e texto devem ser separados e unidirecionais. (✅)

### Descoberta de Serviço (Service Discovery)
* **RF06:** O cliente não deve possuir IPs de brokers pré-configurados (hardcoded). A descoberta deve ser dinâmica.
* **RF07:** O sistema deve possuir um mecanismo de registro dinâmico de brokers (pode ser um *registry* isolado, um broker especial, ou via *broadcast* inicial).
* **RF08:** O cliente deve implementar uma lógica de seleção de broker ao conectar (ex: *round-robin*, menor latência).

---

## 3. Requisitos Não Funcionais (RNF) e Qualidade de Serviço (QoS)
### Controle de Qualidade de Serviço (QoS)
* **RNF01 (Texto):** O sistema deve garantir a entrega das mensagens de texto (implementação de política de *Retry*).
* **RNF02 (Áudio):** A transmissão de áudio deve priorizar a baixa latência, tolerando a perda de pacotes.
* **RNF03 (Vídeo):** A transmissão de vídeo deve usar taxa adaptativa, implementando *drop de frames* e uso de *buffers* simples para evitar gargalos.

### Concorrência e Processamento
* **RNF04:** O cliente deve obrigatoriamente utilizar *threads* para o processamento assíncrono das tarefas.
* **RNF05:** O sistema deve isolar em *threads* separadas as seguintes etapas:
  1. Captura de mídia.
  2. Envio de dados.
  3. Recepção de dados.
  4. Renderização.

### Restrições Tecnológicas
* **RNF06:** O desenvolvimento deve ser feito integralmente em **Python 3**.
* **RNF07:** Toda a comunicação assíncrona deve ser implementada usando a biblioteca **ZeroMQ** (sugerido PUB/SUB entre participantes).
* **RNF08:** A interface pode ser 100% voltada para Desktop (suporte a dispositivos móveis é opcional).

---

## 4. Arquitetura do Sistema e Tolerância a Falhas

### Cluster de Brokers
* **ARQ01:** A arquitetura central deve consistir em N brokers cooperando, com cada broker gerenciando um subconjunto de usuários ou grupos.
* **ARQ02:** O sistema deve implementar o roteamento de mensagens de grupos entre brokers diferentes de forma eficiente, garantindo a prevenção de *loops* de mensagens.
* **ARQ03:** A comunicação inter-brokers deve ser feita utilizando os padrões do ZeroMQ (ex: ROUTER/DEALER ou modelo híbrido PUB/SUB).

### Tolerância a Falhas e Resiliência
* **ARQ04:** O sistema (clientes e brokers) deve implementar um mecanismo de verificação de integridade (*heartbeat*) usando PUB/SUB ou REQ/REP.
* **ARQ05:** O sistema deve utilizar *timeouts* para detectar a queda de um broker.
* **ARQ06:** Ao detectar uma falha, o cliente deve realizar *failover* (reconexão automática a outro broker ativo) mantendo a sessão do usuário com o mínimo de interrupção.

---

## 5. Entregáveis do Projeto
1. **Código-Fonte:**
   * Deve ser modularizado (arquivos/classes separadas para cliente, broker, discovery e media workers).
2. **Demonstração Obrigatória (Simulações):**
   * Provocar a falha intencional de um broker em tempo real.
   * Demonstrar o cliente percebendo a falha e reconectando automaticamente.
   * Demonstrar o tráfego de mensagens fluindo normalmente entre usuários conectados em brokers distintos.
3. **Documentação Técnica:**
   * Documento relatando o projeto, para o qual esta lista de requisitos serve como esqueleto estrutural.
