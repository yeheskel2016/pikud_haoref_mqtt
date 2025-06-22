oref-database: [Grabbed from \data\com.alert.meserhadash\databases]
Using "export_sqlite.py" it's possible to export specific tables to .json format

1.The following table "LocalizableDefinedMessagesRowData" contains all the possible titles that the application has -> we exported only the Hebrew titles to titles.json 
any duplicated title is merged to just one title.

2.The following table "Segment" contains all the possibile cities that the application has, including GPS coordinates for each city,
There is also main cities there i.e "תל אביב - יפו" which is a parent city for :
"תל אביב - דרום העיר ויפו"
"תל אביב - מזרח"
"תל אביב - מרכז העיר"
"תל אביב - עבר הירקון"

I'm not sure if when alert in Tel Aviv happening if they send only the sub ids (the "childs") or also the parent id.. using "Segment_to_cities.py" I took the raw segment data (Segment.json) and converted it to
"cities.json" which has only city->ID format which is what more relevant. (The main cities will have added "ראשי" in their name to indicate it's parent city)


*Small limitation bug for the export_sqlite.py script, it couldnt export the LocalizableDefinedMessagesRowData table properly to .json and I had to manually grab the titles
there.