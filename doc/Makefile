FILEBASENAME ?= deployment-current-state
MERMAID      ?= mmdc
PLANTUML     ?= plantuml
VERBOSE 	 ?=

%.png: %.puml
	${PLANTUML} $(VERBOSE) -pipe < $< > $@

%.png: %.mmd
	${MERMAID} -e png -i $< -o $@

%.svg: %.mmd
	${MERMAID} -e svg -i $< -o $@

%.svg: %.puml
	${PLANTUML} -f svg $(VERBOSE) -pipe < $< > $@

pngFiles = artifact-flow-vintage.png artifact-flow-gitless.png git2oci-architecture.png

all: $(pngFiles)

clean:
	rm -f $(pngFiles)
