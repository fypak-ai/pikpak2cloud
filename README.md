# PikPak Manager

Gerenciador web de arquivos PikPak com suporte a downloads offline.

## Deploy no Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template)

### Variáveis de ambiente

| Variável | Obrigatório | Descrição |
|----------|------------|-----------|
| `DATABASE_URL` | Opcional | PostgreSQL URL (se omitido, usa SQLite local) |
| `PORT` | Auto | Injetado pelo Railway |

### Setup local

```bash
pip install -r requirements.txt
python app.py
```

## Uso

1. Abra o site
2. Cole seu Bearer token do PikPak no campo no topo
3. Clique em **Conectar**
4. Navegue pelos arquivos, faça downloads offline ou gerencie a fila

## Como obter o Bearer token

1. Abra o PikPak no navegador ou app
2. Extraia o token de autenticação da sessão atual
3. Cole no campo da aplicação

## Features

- 📁 Navegação de arquivos e pastas
- ⬇️ Download offline (HTTP, torrent, magnet)
- 🗑️ Exclusão de arquivos (individual ou em lote)
- 📋 Fila de tarefas (banco de dados PostgreSQL ou SQLite)
- 🔒 Autenticação via token Bearer
