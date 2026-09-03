# Organizador

Aplicação local para Windows 11 que vigia Downloads, pede a classificação dos
ficheiros académicos e mantém uma biblioteca pesquisável por disciplina,
tópico e tipo de conteúdo.

## Funcionalidades

- Vigia apenas os novos ficheiros elegíveis na pasta Downloads configurada.
- Move cada ficheiro para uma caixa de entrada segura antes de pedir uma decisão.
- Permite escolher disciplina, tipo de conteúdo, nome final e uma tarefa opcional.
- Organiza sem substituir silenciosamente ficheiros existentes.
- Mantém pesquisa textual local no nome e conteúdo dos formatos suportados.
- Permite importar ficheiros manualmente em lotes limitados.
- Mantém histórico e permite desfazer a organização mais recente.
- Recupera operações interrompidas sem adivinhar quando o estado é ambíguo.
- Permite adotar no catálogo um ficheiro já existente numa disciplina sem o mover.
- Permite marcar ocorrências de reconciliação como revistas sem alterar os ficheiros.
- Vive na área de notificação e pode iniciar com a sessão do Windows.

## Proteção dos ficheiros

O Organizador não substitui nem elimina documentos silenciosamente. Aguarda que
um download deixe de ser temporário e permaneça estável, usa nomes alternativos
como `nome (2).pdf` em caso de colisão e regista cada movimento para permitir
recuperação depois de uma interrupção. Estados ambíguos ficam visíveis para
revisão manual em vez de serem corrigidos por tentativa.

Ficheiros que já estavam em Downloads antes do arranque não são importados
automaticamente. A importação manual exige confirmação e processa no máximo 25
ficheiros de cada vez.

## Instalação no Windows

1. Abre a versão pretendida na página
   [Releases](https://github.com/Parrolas/organizador/releases).
2. Transfere `Organizador-<versão>-windows-x64.zip` e o ficheiro
   `.zip.sha256` com o mesmo nome.
3. Verifica o SHA-256 no PowerShell:

   ```powershell
   Get-FileHash .\Organizador-0.2.0-windows-x64.zip -Algorithm SHA256
   Get-Content .\Organizador-0.2.0-windows-x64.zip.sha256
   ```

4. Confirma que os dois valores são iguais e extrai todo o ZIP para uma pasta
   permanente.
5. Executa `Organizador.exe`. Não movas apenas o executável: a pasta
   `_internal` que o acompanha também é necessária.

O executável ainda não tem assinatura de código. O Microsoft Defender
SmartScreen pode mostrar "O Windows protegeu o PC" na primeira execução. Se o
ZIP veio da página oficial e o SHA-256 coincide, escolhe **Mais informações** e
depois **Executar mesmo assim**. Não ignores o aviso se a origem ou o hash não
forem os esperados.

Na primeira execução, escolhe a pasta Universidade. Por predefinição, a
aplicação usa `Documentos\Universidade`, cria `_Caixa de Entrada` dentro dessa
pasta e vigia a pasta Downloads conhecida pelo Windows. Tudo pode ser alterado
em **Definições**.

A aplicação instala-se sozinha no menu Iniciar: basta procurar
"Organizador" na pesquisa do Windows (Win+S) e clicar no resultado para a
abrir.

## Área de notificação

Fechar a janela principal não termina a aplicação: esconde-a na área de
notificação para continuar a vigiar Downloads. Usa **Sair** no menu do ícone do
Organizador para a terminar completamente.

## Dados e privacidade

O Organizador trabalha localmente. Não envia ficheiros, nomes, conteúdo ou
estatísticas para serviços externos.

Os dados internos ficam em `%LOCALAPPDATA%\Organizador`:

- `settings.json`: definições da aplicação.
- `organizador.db`: catálogo, histórico e índice de pesquisa SQLite.
- `organizador.log`: diagnóstico local com rotação.

Os documentos continuam na pasta Universidade escolhida pelo utilizador. A
aplicação nunca usa a base de dados como cópia dos documentos.

## Atualização e reversão

A app verifica automaticamente se existe uma versão nova no arranque (podes
desativar isto nas Definições). Quando existe, aparece "Instalar atualização"
no menu do tabuleiro; um clique transfere, verifica o SHA-256 publicado e a
versão do pacote, prepara a atualização numa área isolada e só depois reinicia
para aplicar. Um assistente dedicado espera que a app antiga termine, troca as
pastas com verificação de cada passo e só confirma quando a nova versão
arranca com sucesso. Os teus dados ficam sempre em
`%LOCALAPPDATA%\Organizador` e nunca são tocados pela atualização.

Antes de qualquer migração da base de dados, a app cria uma cópia de
segurança automática (base de dados e definições) e só a remove depois de um
arranque saudável. Se a nova versão falhar antes desse ponto, a versão e os
dados anteriores são repostos automaticamente e o resultado é mostrado uma vez
no arranque seguinte. Depois do ponto de saúde, nunca há reposição automática
de dados: a versão anterior é mantida para recuperação manual.

A versão anterior é mantida numa pasta de segurança até o novo arranque
correr com sucesso, servindo de reversão imediata se algo correr mal.

Antes de atualizar manualmente:

1. Usa **Sair** no ícone da área de notificação.
2. Faz uma cópia de segurança de `%LOCALAPPDATA%\Organizador`.
3. Conserva o ZIP da versão atual até confirmares a nova versão.
4. Extrai a nova versão para uma pasta nova e executa-a.

As migrações da base de dados são automáticas. Para reverter, termina a
aplicação, volta ao ZIP anterior e restaura também a cópia dos dados feita por
essa versão. Não mistures uma base de dados já migrada com um executável mais
antigo. Os documentos da pasta Universidade não precisam de ser restaurados.

## Desinstalação

1. Em **Definições**, desativa **Iniciar o Organizador quando entro no Windows**
   e guarda.
2. Usa **Sair** no ícone da área de notificação.
3. Elimina a pasta onde extraíste a aplicação.
4. Se também quiseres apagar o catálogo, histórico, definições e logs, elimina
   `%LOCALAPPDATA%\Organizador`.

A desinstalação não deve eliminar a pasta Universidade nem os documentos nela
guardados.

## Limitações atuais

- PDFs digitalizados apenas como imagem precisam de OCR e não entram na pesquisa textual.
- `.doc`, `.ppt`, `.xls` e ficheiros OneNote podem ser organizados, mas o conteúdo não é indexado.
- Documentos com mais de 50 MB são organizados sem indexação para limitar memória em segundo plano.
- As sugestões aprendidas dependem de padrões de nome repetidos e continuam a exigir confirmação.

## Desenvolvimento

Requisitos: Windows 11 e Python 3.11 ou superior. Os builds oficiais usam Python 3.13.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
.\.venv\Scripts\python.exe -m organizador.main
```

Validação individual:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest
```

## Build local

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

O script executa lint, verificação de formato, mypy, testes, PyInstaller e um
arranque de diagnóstico do pacote. Depois adiciona as licenças e produz:

- `dist\Organizador\Organizador.exe`
- `dist\releases\Organizador-<versão>-windows-x64.zip`
- `dist\releases\Organizador-<versão>-windows-x64.zip.sha256`

As dependências exatas da versão são fixadas em `constraints-release.txt`.
`pyproject.toml` é a fonte única das dependências diretas. `defusedxml` é mantido
explicitamente porque o `openpyxl` o ativa para proteger a leitura de folhas de
cálculo XML não confiáveis.

## Publicação

Tags no formato `vMAJOR.MINOR.PATCH` ativam o workflow de release. O workflow
confirma que a tag coincide com `organizador.__version__`, recria o ambiente a
partir de `constraints-release.txt`, executa toda a validação e publica o ZIP e
o SHA-256 numa nova GitHub Release. Uma release já publicada não é substituída
por uma repetição do workflow.

Antes de publicar, o workflow executa a atualização real com o código exato da
versão anterior (`scripts\run_update_release_e2e.ps1`) contra os bytes do
candidato. A release é criada como prerelease e só é promovida a estável
manualmente, depois de confirmada a transição a partir da base instalada. Uma
versão anterior com um atualizador inseguro nunca recebe uma atualização
automática que não consiga aplicar.

## Licenças

O código do Organizador é distribuído sob a licença MIT em `LICENSE`. O pacote
Windows inclui componentes de terceiros com licenças próprias, documentados em
`LICENSES/THIRD-PARTY-NOTICES.md` e nos respetivos textos de licença.
