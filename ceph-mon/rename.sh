#!/bin/bash
charm=$(grep "charm_build_name" osci.yaml | awk '{print $2}')
echo "renaming ${charm}_*.charm to ${charm}.charm"
echo -n "pwd: "
pwd
ls -al
echo "Removing bad downloaded charm maybe?"
if [[ -e "${charm}.charm" ]];
then
    rm "${charm}.charm"
fi
echo "Renaming charm here."
if [[ -e "${charm}_amd64.charm" ]]; then
    mv "${charm}_amd64.charm" "${charm}.charm"
else
    latest_charm=$(ls -t ${charm}_*.charm | head -n1)
    mv "${latest_charm}" "${charm}.charm"
fi
