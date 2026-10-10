# supabase-migrate

Migração de projeto Supabase, executada por um único comando na raiz:

```bash
./migrate
```

**Estado: testes automatizados e integração PostgreSQL no CI; a execução completa em Supabase ainda precisa de homologação.** Não promete clonagem de 100% da plataforma ou atomicidade global. PostgreSQL, Storage, Edge Functions e Management API não participam da mesma transação. O banco é substituído em uma transação; as outras etapas têm recuperação compensatória por backup, que também pode falhar. Recursos sem um caminho de cópia confiável bloqueiam a operação antes da limpeza.

## Uso

Requisitos: Linux/macOS/WSL, Bash, Python **3.10+**, clientes PostgreSQL (`psql`, `pg_dump`, `pg_restore`) da mesma major do servidor e Supabase CLI com `functions download/deploy --use-api` (comandos conferidos na versão **2.120.0**). Não há dependências Python, Docker nem instalação automática de pacotes.

1. Clone este repositório e entre na pasta.
2. Execute `./migrate`.
3. Informe o ID de origem e, em seguida, o ID de destino.
4. Informe o Personal Access Token e as duas URLs PostgreSQL com senha, em prompts ocultos. As URLs ficam em **Connect** no Dashboard. Use conexão direta ou **Session pooler**, porta **5432**; o transaction pooler não serve para snapshots.
5. Leia o inventário, a estimativa, os limites e os bloqueios encontrados.
6. Isole o destino de clientes, gravações e jobs. Confirme digitando `MIGRAR <ID_DESTINO>`.

O PAT precisa de acesso aos dois projetos, leitura de configurações/chaves/funções e permissão de escrita no destino. Obtenha-o em <https://supabase.com/dashboard/account/tokens>. A autenticação do MCP neste chat **não é herdada** pelo script. O script passa o PAT ao CLI por variável de ambiente, sem colocá-lo na linha de comando. Não precisa executar `supabase link` e não altera o vínculo de outro diretório.

Opcionalmente, forneça credenciais pelo ambiente: `SUPABASE_ACCESS_TOKEN`, `SOURCE_DB_URL`, `TARGET_DB_URL`. Evite salvar valores no histórico do terminal. IDs podem ser passados com `--source` e `--target`; a confirmação destrutiva continua obrigatória. Não há `--yes`.

```bash
# Apenas inventário e estimativa. Não apaga nem importa recursos.
./migrate --plan

# Diff dos estados atuais, incluindo checksums das tabelas. Somente leitura.
./migrate --diff --source ID_ORIGEM --target ID_DESTINO

# Estimativa usando uma taxa nominal de 25 MiB/s.
./migrate --throughput-mib 25

# Recusa imediatamente, pois a plataforma não oferece transação global.
./migrate --require-atomic

# Recuperação após interrupção ou falha na recuperação automática.
./migrate --recover .migrate/AAAAmmddTHHMMSSZ-identificador
```

## Fluxo

Inventário → estimativa → confirmação → backup do destino → captura da origem → rechecagem de alterações → limpeza de Functions/Storage no destino → limpeza/restauração transacional do banco → configurações → arquivos/owners → Functions → verificação.

A limpeza começa **depois** de verificar os backups. O script não apaga o projeto Supabase, não recria seu ID e não altera a origem. Conexões à origem têm `default_transaction_read_only=on`; o cliente HTTP da origem permite somente GET. Leituras de objetos podem atualizar telemetria de acesso mantida pela plataforma.

Cada captura usa um snapshot PostgreSQL exportado, compartilhado entre dumps e checksums. A origem permanece online e pode receber gravações; **gravações posteriores ao snapshot não entram na migração**. Storage não dispõe de snapshot conjunto com o banco: o script compara inventário/versões depois do download e aborta se detectar alterações. Isso não equivale a CDC, sincronização contínua ou consistência distribuída perfeita.

As transações de snapshot são encerradas antes do download de arquivos e bundles, para não manter uma transação PostgreSQL aberta durante toda a transferência. Na restauração, o schema da aplicação é criado primeiro, os dados Auth e de aplicação vêm em seguida e as constraints/triggers são aplicadas depois. Isso preserva referências a `auth.users`.

A estimativa é uma faixa heurística, não um SLA: considera tamanho do banco, quantidade/tamanho de objetos, funções, backup, restore e verificações, usando 10 MiB/s por padrão. A captura e o cálculo dos checksums consomem I/O da origem. O inventário de metadados fica em memória; arquivos são transferidos em streaming para disco.

## Cobertura e limites

| Recurso | Tratamento |
| --- | --- |
| Schemas de aplicação, tabelas, dados, índices, views, sequences, tipos, SQL functions, triggers, grants e RLS | Dump/restore nativo, incluindo owners, sem `--no-owner`. Schemas gerenciados não são apagados. |
| Histórico CLI `supabase_migrations` | Incluído no dump de aplicação. |
| Auth | Dados das tabelas, incluindo usuários, UUIDs e hashes de senha; schema gerenciado mantido. Versões/tabelas incompatíveis bloqueiam. |
| Customizações auth/storage | Policies, triggers, ACLs e flags RLS; funções e tabelas customizadas nesses schemas bloqueiam. Privilégios padrão das roles `supabase_*` são preservados e precisam coincidir entre projetos. |
| Realtime | Publicações da aplicação, associação de tabelas e configuração exposta pela Management API. Publicações internas `supabase_realtime_*` e partições gerenciadas permanecem no destino; `supabase_realtime` é migrada. Conexões/mensagens transitórias não são copiadas. |
| Storage padrão | Buckets, bytes dos objetos, IDs, `owner`, `owner_id`, timestamps e `user_metadata`; preserva versão física/ETag criados no destino. Valida bytes por SHA-256. |
| Edge Functions | Download e deploy via CLI, preservando `verify_jwt` e nome. Exige `index.ts` e bundle sem import map declarado. Dependências não recuperáveis bloqueiam antes da limpeza. |
| Secrets de Edge Functions | Somente nomes no relatório e no final. Valores não são exportados, criados ou apagados. Secrets já existentes no destino permanecem e precisam de revisão. |
| Auth, SMTP/OAuth, Storage, Realtime e Data API | Copia campos de configuração graváveis definidos no contrato OpenAPI; valor mascarado detectado bloqueia. |
| Roles customizadas sem LOGIN | Atributos/memberships; incompatibilidade, role privilegiada, role exclusiva do destino ou senha não exportável bloqueiam. |
| Extensões | Devem existir nos dois projetos com versões iguais. Não se substituem extensões/plataforma implicitamente. |
| Cron, Vault/pgsodium com dados, Storage vetorial/analítico, upload multipart pendente, SSO/Third-party Auth, subscriptions, FDW, large objects e event triggers customizados | **Bloqueiam**, exigindo adaptadores e homologação específicos. Não são ignorados e a migração não é declarada completa. |
| Chaves API/JWT, ID/URL de projeto, domínio/DNS, região, plano, compute, rede/SSL, backups/PITR, logs e integrações externas | Identidades/infraestrutura do destino são mantidas. Configuração externa não é clonada. |

Filas/tabelas normais em schemas de aplicação passam pelo dump nativo. Extensões com armazenamento externo, schemas protegidos ou requisitos específicos precisam de homologação própria; não há garantia universal de migração de toda extensão PostgreSQL.

Tokens JWT emitidos pela origem não passam a valer no destino, embora os registros Auth tenham sido copiados. Provedores OAuth precisam aceitar o callback do destino. O script não substitui o ID antigo dentro de SQL, URLs, dados ou código arbitrários: isso pode alterar o significado da aplicação. Revise essas referências antes do cutover.

A presença de secrets manuais gera o estado `completed_pending_manual_secrets`. Nenhum cliente deve ser direcionado ao destino antes de ajustar secrets/callbacks e homologar a aplicação. Functions que dependem dos secrets podem não funcionar enquanto isso.

## Falhas e recuperação

A restauração do banco usa `psql --single-transaction -v ON_ERROR_STOP=1`. Uma falha nessa transação não deixa metade do banco importada. Storage/configurações/Functions têm efeitos imediatamente visíveis, portanto o destino deve permanecer isolado durante toda a execução. O script exige a declaração do operador, mas não consegue garantir isolamento global de todos os serviços.

Depois da primeira mutação, qualquer erro inicia uma tentativa de recuperação do backup do destino. Se ela passar, a execução **continua retornando erro**, com estado `failed_recovered`. Se falhar, o estado será `recovery_failed`; preserve os backups e use `--recover`. Em caso de SIGKILL/queda da máquina, a recuperação é manual pelo mesmo comando. Não há retomada cega de uma fase destrutiva. Secrets não são alterados e não precisam de rollback pelo script.

Backups, bundles, configurações e relatórios ficam em `.migrate/<execução>/`, ignorada pelo Git e criada com permissões privadas (diretório 0700/arquivos 0600). **Contêm dados pessoais, hashes de senha e possivelmente credenciais de configuração; não são criptografados em repouso pelo programa.** Use disco protegido e uma política de retenção. Não os envie ao GitHub. Hashes detectam corrupção acidental; não substituem autenticação contra alguém que possa reescrever também o manifesto.

O lock local evita duas execuções no mesmo diretório para o mesmo destino; não é um lock distribuído. Mantenha somente um operador por destino. Relatórios não contêm valores de secrets; erros de subprocessos são sanitizados para não imprimir dados sensíveis.

## Relatório e diff

Ao terminar uma migração, um plano ou um `--diff`, o script grava `final-report.md` e `final-report.json` no diretório privado da execução. Também tenta gerar esses arquivos quando a operação falha após abrir o diretório da execução. O relatório mostra o estado de cada recurso, contagens de tabelas/linhas/usuários/arquivos/owners/funções, nomes dos secrets manuais e quatro comparações:

- Snapshot capturado × destino verificado: evidência da cópia executada.
- Origem atual × destino atual: diferenças observadas ao final.
- Snapshot × origem atual: gravações ou alterações posteriores à captura.
- Destino anterior × destino atual: efeito da operação no destino.

As observações têm horários próprios: a origem continua online, portanto igualdade instantânea entre serviços não é garantida. Divergência atual não anula automaticamente uma cópia correta do snapshot. `--plan` não calcula checksums nem conta linhas: registra `data_not_checked`, sem presumir igualdade dos dados. `--diff` lê todas as linhas das tabelas migráveis para calcular fingerprints; pode consumir I/O e deve ser executado em horário adequado à carga da aplicação.

O diff identifica tabelas e recursos ausentes ou alterados, sem exportar valores de linhas, configurações, SQL, owners ou nomes dos arquivos. Objetos Storage aparecem por identificador opaco derivado do caminho. Os detalhes são limitados a 200 chaves por categoria, com contagens totais e indicação de truncamento. O diff atual de Storage compara metadados; os bytes são verificados por SHA-256 durante a migração. Hashes de bundles Edge são informativos, pois o empacotamento pode conter IDs do projeto; nenhuma Function é invocada automaticamente.

Se a coleta do diff final falhar, o relatório registra `not_collected` e a execução retorna código 2. Isso **não desfaz** uma migração já concluída. Consulte `migration_phase` e `migration_completed` para distinguir falha de cópia de comparação incompleta. Relatórios finais não incluem valores de secrets; backups e outros arquivos da execução continuam sensíveis e não devem ser compartilhados.

`--recover` preserva o relatório da migração e gera outro em `<execução>/recovery-<id>/`. Esse modo observa apenas o destino recuperado, pois não solicita credenciais da origem; execute `--diff` separadamente para comparar ambos.

## Validação

```bash
python3 -m unittest discover -s tests -v
bash -n migrate
./migrate --help
```

Os testes unitários usam dublês de banco/API para verificar guards, fluxos de falha/recuperação, confirmação, backup corrompido, owners e relatórios sem vazamento de valores. O CI também executa PostgreSQL 17 em bancos descartáveis para testar proteção da origem, snapshot compartilhado, restauração com FK para Auth, RLS, extensão em `public`, preservação do Realtime interno e rollback de falha injetada. Esses testes de integração ficam desabilitados por padrão localmente e nunca recebem credenciais dos projetos reais. Não substituem um teste real de permissões do Supabase, quotas, bundles e OAuth. Veja [homologação](docs/HOMOLOGACAO.md).

Contratos consultados: [backup/restore oficial](https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore), [Management API](https://supabase.com/docs/reference/api/introduction), [OpenAPI](https://github.com/supabase/supabase/blob/master/apps/docs/spec/api_v1_openapi.json) e ajuda da CLI 2.120.0. A tentativa de consultar `https://supabase.com/changelog.md` ficou indisponível no ambiente de desenvolvimento; os contratos foram conferidos pelas ferramentas de documentação e GitHub.
