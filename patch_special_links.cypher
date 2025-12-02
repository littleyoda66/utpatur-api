// Unna Allakas Fjällstuga -> Hotell Riksgränsen (valeurs corrigées)

MATCH (a:Hut {name:"Unna Allakas Fjällstuga"}),
      (b:Hut {name:"Hotell Riksgränsen"})

MERGE (a)-[l1:LINK]->(b)
SET l1.distance_km = 30,
    l1.dplus_m     = 556,
    l1.dminus_m    = 757

MERGE (b)-[l2:LINK]->(a)
SET l2.distance_km = 30,
    l2.dplus_m     = 757,   // inversé
    l2.dminus_m    = 556;    // inversé
