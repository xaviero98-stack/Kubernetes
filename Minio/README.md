# Minio
Minio is also deployed via the Minio oficial operator for the Minio community version which is straight away Minio, now there's also a payed service called AI Stor or something like that but it's not the main open source.
As you will also see, Minio obviously needs PVs created on the nodes before we deploy them. There's a way to allow dinamic PV creation but it requieres further integration with other technologies and if you have a 3 nodes cluster manually making them and subsequently assigning them to the the minio tenant is the best option.
In this case, the configuration seen on the yamls is pretty straightforward and self-explanatory.
