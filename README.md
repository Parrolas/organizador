# Organizador

Aplicação local para Windows que vigia novos ficheiros de estudo em `Downloads`,
move-os para uma Caixa de Entrada segura e pergunta em que disciplina e tipo de
documento devem ficar.

## O que já funciona

- Ícone no tabuleiro do sistema e vigilância em segundo plano.
- Deteção de downloads terminados no Chrome, Edge e Firefox.
- Caixa de Entrada em `Universidade\_Caixa de Entrada`.
- Escolha rápida de disciplina e tipo: Slides, Exercícios, Testes, Trabalhos ou
  Outros.
- Sugestão de destino a partir do nome do ficheiro e de escolhas anteriores
  confirmadas pelo utilizador.
- Renomeação segura: a extensão original é preservada e ficheiros existentes
  nunca são substituídos.
- Devolver um ficheiro que não seja da universidade a `Downloads`.
- Desfazer a última organização.
- Pesquisa local dentro de PDF, DOCX, PPTX, XLSX, TXT, Markdown, CSV e notebooks
  Jupyter.
- Tarefas, prazos e notificações para trabalhos vencidos ou a vencer hoje.
- Arranque opcional com o Windows, sem permissões de administrador.
- Tema escuro em toda a aplicação e no popup de organização.
- Popup no canto inferior esquerdo para não ficar atrás das notificações do Windows.
- Interface em português de Portugal.

## Proteção dos ficheiros

O Organizador nunca apaga documentos. Um ficheiro só é movido depois de:

1. deixar de ter uma extensão temporária como `.crdownload` ou `.part`;
2. manter o mesmo tamanho em várias leituras;
3. poder ser aberto exclusivamente no Windows, sinal de que o browser terminou;
4. encontrar um destino que não exista; em caso de colisão cria `nome (2).pdf`;
5. conseguir registar o movimento na base de dados. Se o registo falhar, o
   movimento é revertido.

Os documentos e o índice de pesquisa ficam apenas neste computador.

## Instalação para desenvolvimento

Requisitos: Windows 10/11 e Python 3.11 ou superior.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\run.ps1
```

O script cria `.venv`, instala a aplicação e as ferramentas de qualidade. A
primeira abertura pede a pasta Universidade e a primeira disciplina. A pasta
proposta é `Documentos\Universidade`.

Para arrancar diretamente no tabuleiro do sistema:

```powershell
.\scripts\run.ps1 -Background
```

## Criar o executável

```powershell
.\scripts\build.ps1
```

O resultado fica em `dist\Organizador\Organizador.exe`. O build executa `ruff`,
`mypy`, os testes e um arranque real do executável antes de terminar. Como esta
versão não tem assinatura de código, o Windows SmartScreen pode mostrar um
aviso na primeira execução.

## Testes manuais rápidos

1. Abre a app e cria uma disciplina com código e palavras-chave.
2. Descarrega um PDF pequeno cujo nome contenha esse código.
3. Confirma que desaparece de `Downloads` apenas depois de terminar.
4. Escolhe a disciplina e `Slides` no popup.
5. Confirma o destino em `Universidade\<Disciplina>\Slides`.
6. Usa `Desfazer última organização` no menu do tabuleiro.
7. Organiza dois ficheiros com o mesmo padrão de nome e confirma que o terceiro
   recebe a mesma sugestão, mas continua a exigir confirmação.

## Qualidade

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m mypy src\organizador
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest
```

Os testes usam apenas pastas temporárias. Nunca abrem nem alteram o `Downloads`
real.

## Dados locais

- Definições: `%LOCALAPPDATA%\Organizador\settings.json`
- Base de dados e índice: `%LOCALAPPDATA%\Organizador\organizador.db`
- Registo técnico: `%LOCALAPPDATA%\Organizador\organizador.log`
- Documentos: a pasta Universidade escolhida pelo utilizador

## Limitações atuais

- Ficheiros que já estavam em `Downloads` antes de a app arrancar não são
  movidos automaticamente. Isto evita reorganizar centenas de ficheiros sem
  confirmação.
- PDFs digitalizados apenas como imagem precisam de OCR e ainda não aparecem na
  pesquisa textual.
- Os formatos Office antigos (`.doc`, `.ppt`, `.xls`) e ficheiros OneNote podem
  ser organizados, mas o conteúdo interno não é indexado.
- Documentos com mais de 50 MB são organizados sem indexação para limitar o uso
  de memória em segundo plano.
- A aprendizagem só reconhece padrões de nome repetidos e concordantes; nomes
  genéricos ou escolhas em conflito mantêm a sugestão normal.

## Estrutura técnica

- `watcher.py` e `stabilizer.py`: eventos do Windows e conclusão do download.
- `filer.py`: única camada autorizada a mover ficheiros.
- `db.py`: SQLite, histórico, escolhas confirmadas, tarefas e FTS5.
- `classifier.py`: sugestões transparentes por nome e padrões já confirmados.
- `extractors.py` e `indexer.py`: extração de texto e indexação em background.
- `controller.py`: coordenação entre threads e Qt.
- `ui/`: onboarding, popup, tabuleiro e páginas da aplicação.
