.PHONY: all up down status ssh gui logs follow web terminal save test

all: status

up:
	./bin/server up

status:
	./bin/server status

ssh:
	./bin/server ssh

gui:
	./bin/server gui xeyes

logs:
	./bin/server logs

follow:
	./bin/server follow

web:
	./bin/server web

terminal:
	./bin/server terminal

save:
	./bin/server save

stop:
	./bin/server stop

code:
	./bin/server code

test:
	./bin/test-connection.py
