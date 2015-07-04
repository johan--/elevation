all: openstreetmap-carto.xml
	# $(MAKE) -C data/osm
	$(MAKE) -C data/height
	# $(MAKE) -C tilemill/project/osm-tilemill
	$(MAKE) -C mapnik-stylesheets

openstreetmap-carto.xml: openstreetmap-carto/project.mml openstreetmap-carto/*.mss
	carto $< > $@
