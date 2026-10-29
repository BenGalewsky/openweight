# Redesign of Samples Architecture
Our current implementation of `samples` doesn't match the way the researchers work. When planning for a site visit
they request a range of bottle numbers from the lab. These bottles are assigned to the visit, and then when collecting
data for a specific site they will allocate a bottle from that range and store the sample in it.

# Database change
The `samples` table as a foreign key relation to StationVisit we will continue to allow nulls for this relation and add 
a new non-nullable foreign key relation to `visits`

# Allocate Bottles to a Visit
On the visit @/visits/[id] page add a new button "Allocate Bottles" - this will bring up a modal panel where the user 
enters a site short name a starting bottle number and the count of bottles to allocate. This will create rows in the 
`samples` table with null siteVisit values. The sample name will conisist of the site short code an underscore and the 
sequential bottle number from the range.

Add a table to the visit page to show all of the bottles allocated to this visit, sorted by sample_name. Show the
assigned station name or indicate that the bottle is unassigned.

# Use a bottle at a station
On the station visit page visits/[id]/station-visits/[svId] update the "Add Sample" modal panel. Sample name should now 
be a drop down, with the choices filtered to only those samples for this visit where stationVisit relation is null.

The grid on that page should show the list of samples where the staionVisit relation matches the page.


