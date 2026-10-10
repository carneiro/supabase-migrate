# Homologação em projetos descartáveis

Esta implementação ainda não foi executada contra dois projetos Supabase reais. A API é evolutiva; os testes locais verificam contratos de controle, não autorizam presumir compatibilidade de todos os objetos remotos.

Use dois projetos sem dados de produção, mesma major PostgreSQL, mesmas extensões/versões e mesma versão interna de Auth/Storage. Verifique também quotas de armazenamento, tamanho máximo de upload e Functions no plano do destino.

1. Na origem, crie tabelas com FK circular, partitions, sequences/identity, tipos, índices, views, policies restritivas/permissivas, funções e triggers. Inclua nomes com espaços e aspas.
2. Cadastre usuários Auth e identidades. Configure o mesmo provedor de teste e registre o callback do destino. Inclua dados que referenciem `auth.users`.
3. Crie buckets público/privado, arquivos vazios, binários, nomes Unicode/com espaços e owners UUID e textuais. Configure MIME types e limites por bucket.
4. Habilite Realtime em algumas tabelas. Publique Functions com e sem `verify_jwt`, uma com nome de exibição distinto e uma que dependa de secret manual.
5. Coloque dados diferentes no destino. Mantenha somente o operador conectado, sem clientes, gravações e jobs. Guarde também um backup independente.
6. Execute `./migrate --plan`. Verifique a cobertura e resolva todos os bloqueios sem usar dados reais.
7. Execute `./migrate`. Confira o relatório final, login novo com a senha anterior, UUIDs, hashes/contagens, RLS para usuários diferentes, memberships/ACLs, Realtime, download autorizado/negado e owners dos arquivos.
8. Insira os secrets manualmente pelo Dashboard/CLI no destino; então invoque as Functions com autenticação correta e confira seus resultados. A invocação não é automatizada porque pode produzir efeitos externos.
9. Em uma execução separada, provoque uma falha de API depois da limpeza. Confirme retorno diferente de zero, recuperação dos dados/arquivos/Functions/configurações antigos e estado `failed_recovered`.
10. Teste interrupção abrupta e `./migrate --recover <execução>`. Confirme que backup corrompido é recusado, e que uma falha de recuperação mantém `recovery_failed`.
11. Provoque mudança em um objeto Storage durante a captura: a execução deve parar antes da limpeza. Altere dados do destino durante o backup: também deve parar.
12. Prove os bloqueios: projetos iguais, URL de outro projeto, porta 6543, versões incompatíveis, extensões divergentes, Vault com dados, upload multipart em andamento, SSO, Cron, import map e roles LOGIN customizadas.

Não retire um bloqueio apenas para fazer a migração passar. Adicione o adaptador correspondente, inclua backup/restore/verify/recover e homologue o recurso em ambos os sentidos.

O critério de conclusão técnica é o backup íntegro e a verificação do conjunto suportado; o critério de liberação da aplicação inclui também secrets, callbacks, URLs e testes funcionais. Uma cópia concluída não constitui um cutover atômico da aplicação.
