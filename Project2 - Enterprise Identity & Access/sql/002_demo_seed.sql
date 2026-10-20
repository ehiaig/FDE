-- Fictional fixtures. Execute as database owner, never from the web process.
INSERT INTO users(id, tenant, external_id, username) VALUES
('10000000-0000-0000-0000-000000000001','acme','demo|alice','alice@acme.example'),
('10000000-0000-0000-0000-000000000002','acme','demo|morgan','morgan@acme.example'),
('20000000-0000-0000-0000-000000000001','globex','demo|sam','sam@globex.example'),
('20000000-0000-0000-0000-000000000002','globex','demo|riley','riley@globex.example');
INSERT INTO groups(id, tenant, name) VALUES
('10000000-0000-0000-0000-000000000010','acme','Managers'),
('20000000-0000-0000-0000-000000000010','globex','Managers');
INSERT INTO memberships(tenant,group_id,user_id) VALUES
('acme','10000000-0000-0000-0000-000000000010','10000000-0000-0000-0000-000000000002'),
('globex','20000000-0000-0000-0000-000000000010','20000000-0000-0000-0000-000000000002');
INSERT INTO documents(id,tenant,title,body,required_role) VALUES
('10000000-0000-0000-0000-000000000101','acme','Acme leave policy','Acme employees receive 25 days of annual leave. Submit leave requests to your manager.','employee'),
('10000000-0000-0000-0000-000000000102','acme','Acme management budget','Acme managers have a £120,000 annual team development budget. This is management-only information.','manager'),
('20000000-0000-0000-0000-000000000101','globex','Globex leave policy','Globex employees receive 30 days of annual leave. Submit leave requests through People Operations.','employee'),
('20000000-0000-0000-0000-000000000102','globex','Globex management budget','Globex managers have a £80,000 annual team development budget. This is management-only information.','manager');
